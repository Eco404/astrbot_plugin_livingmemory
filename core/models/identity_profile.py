"""Optional participant hints used to disambiguate memory-generation prompts."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .platform_identity import canonical_platform, normalize_platform_token


@dataclass(frozen=True, slots=True)
class SupplementalIdentityProfile:
    """User-supplied hints that may supplement, but never override, source evidence."""

    user_id: str
    platform: str = ""
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    gender: str = ""
    pronouns: tuple[str, ...] = ()
    notes: str = ""
    platform_aliases: tuple[str, ...] = ()
    platform_instances: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "SupplementalIdentityProfile":
        user_id = str(value.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("supplemental identity profile requires user_id")
        raw_platform = str(value.get("platform") or "").strip()
        canonical = canonical_platform(raw_platform)
        aliases = list(cls._strings(value.get("platform_aliases")))
        if raw_platform and normalize_platform_token(raw_platform) != canonical:
            aliases.insert(0, raw_platform)
        profile = cls(
            user_id=user_id,
            platform=canonical,
            display_name=str(value.get("display_name") or "").strip(),
            aliases=cls._strings(value.get("aliases")),
            gender=str(value.get("gender") or "").strip(),
            pronouns=cls._strings(value.get("pronouns")),
            notes=str(value.get("notes") or "").strip(),
            platform_aliases=tuple(dict.fromkeys(aliases)),
            platform_instances=cls._strings(value.get("platform_instances")),
        )
        profile._validate_lengths()
        return profile

    @staticmethod
    def _strings(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            values = [part.strip() for part in value.split("/")]
        elif isinstance(value, (list, tuple, set)):
            values = [str(part).strip() for part in value]
        else:
            values = []
        return tuple(dict.fromkeys(part for part in values if part))

    @property
    def key(self) -> tuple[str, str]:
        return self._normalize(self.platform), self.user_id.casefold()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value for value in (self.display_name, *self.aliases) if value
            )
        )

    def matches_message(
        self,
        *,
        sender_id: str | None,
        platform: str | None,
        sender_name: str | None,
    ) -> bool:
        # Kept in the signature for callers that already supply display names.
        # Display names never participate in identity matching.
        _ = sender_name
        sender_id = str(sender_id or "").strip()
        return bool(
            sender_id
            and sender_id.casefold() == self.user_id.casefold()
            and self._platform_matches(platform)
        )

    def matches_actor_id(self, actor_id: Any) -> bool:
        """Match only a stable actor ID; display names are never identity anchors."""
        raw_actor_id = str(actor_id or "").strip()
        if not raw_actor_id or raw_actor_id.casefold().startswith("unresolved:"):
            return False
        parts = raw_actor_id.split(":", 2)
        if len(parts) != 3 or parts[1].casefold() != "human":
            return False
        platform, _, user_id = parts
        if user_id.casefold() != self.user_id.casefold():
            return False
        expected_platform = canonical_platform(self.platform)
        return not expected_platform or canonical_platform(platform) == expected_platform

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "platform": self.platform,
                "user_id": self.user_id,
                "display_name": self.display_name,
                "aliases": list(self.aliases),
                "gender": self.gender,
                "pronouns": list(self.pronouns),
                "notes": self.notes,
            }.items()
            if value not in ("", [], ())
        }

    def to_storage_dict(self) -> dict[str, Any]:
        payload = self.to_prompt_dict()
        if self.platform_aliases:
            payload["platform_aliases"] = list(self.platform_aliases)
        if self.platform_instances:
            payload["platform_instances"] = list(self.platform_instances)
        return payload

    def _validate_lengths(self) -> None:
        limits = {
            "user_id": (self.user_id, 256),
            "platform": (self.platform, 100),
            "display_name": (self.display_name, 200),
            "gender": (self.gender, 100),
            "notes": (self.notes, 2000),
        }
        for field_name, (value, limit) in limits.items():
            if len(value) > limit:
                raise ValueError(
                    f"supplemental identity {field_name} exceeds {limit} characters"
                )
        if len(self.aliases) > 20 or any(len(value) > 200 for value in self.aliases):
            raise ValueError("supplemental identity aliases exceed allowed limits")
        if len(self.pronouns) > 10 or any(
            len(value) > 100 for value in self.pronouns
        ):
            raise ValueError("supplemental identity pronouns exceed allowed limits")
        if len(self.platform_aliases) > 30 or any(
            len(value) > 100 for value in self.platform_aliases
        ):
            raise ValueError("supplemental identity platform aliases exceed limits")
        if len(self.platform_instances) > 30 or any(
            len(value) > 256 for value in self.platform_instances
        ):
            raise ValueError("supplemental identity platform instances exceed limits")

    def _platform_matches(self, platform: str | None) -> bool:
        expected = canonical_platform(self.platform)
        actual = canonical_platform(platform)
        if not expected or not actual:
            return True
        return expected == actual

    @staticmethod
    def _normalize(value: Any) -> str:
        return normalize_platform_token(value).replace("_", "")


def parse_supplemental_identity_profiles(
    raw: Any,
) -> list[SupplementalIdentityProfile]:
    """Validate serialized or in-memory profile records."""
    if isinstance(raw, dict):
        raw = raw.get("profiles", [])
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    if not isinstance(raw, list):
        raise ValueError("supplemental identity profiles must be an array")
    profiles = [
        SupplementalIdentityProfile.from_mapping(item)
        for item in raw
        if isinstance(item, dict)
    ]
    if len(profiles) != len(raw):
        raise ValueError("each supplemental identity profile must be a JSON object")
    for index, profile in enumerate(profiles):
        for other in profiles[:index]:
            if profile.user_id.casefold() != other.user_id.casefold():
                continue
            if profile._platform_matches(other.platform):
                raise ValueError(
                    "supplemental identity profiles contain overlapping "
                    "platform/user_id entries"
                )
    return profiles


class SupplementalIdentityStore:
    """Small atomic JSON store shared by WebUI and generation pipelines."""

    VERSION = 3

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        profiles: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._profiles: tuple[SupplementalIdentityProfile, ...] = ()
        self.updated_at: float | None = None
        self.load_error = ""
        if profiles is not None:
            self.replace_profiles(list(profiles), persist=False)
        elif self.path is not None:
            self.load()

    @property
    def profiles(self) -> list[SupplementalIdentityProfile]:
        return list(self._profiles)

    def load(self) -> list[SupplementalIdentityProfile]:
        self.load_error = ""
        if self.path is None or not self.path.exists():
            self._profiles = ()
            self.updated_at = None
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw_profiles = (
                payload.get("profiles", []) if isinstance(payload, dict) else payload
            )
            profiles = parse_supplemental_identity_profiles(raw_profiles)
            self._profiles = tuple(profiles)
            self.updated_at = (
                float(payload.get("updated_at"))
                if isinstance(payload, dict) and payload.get("updated_at") is not None
                else self.path.stat().st_mtime
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._profiles = ()
            self.updated_at = None
            self.load_error = str(exc)
        return self.profiles

    def replace_profiles(
        self,
        raw_profiles: list[dict[str, Any]],
        *,
        persist: bool = True,
    ) -> list[SupplementalIdentityProfile]:
        if len(raw_profiles) > 200:
            raise ValueError("supplemental identity profiles cannot exceed 200 items")
        profiles = parse_supplemental_identity_profiles(raw_profiles)
        updated_at = time.time()
        if persist and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": self.VERSION,
                "updated_at": updated_at,
                "profiles": [profile.to_storage_dict() for profile in profiles],
            }
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        self._profiles = tuple(profiles)
        self.updated_at = updated_at
        self.load_error = ""
        return self.profiles

    def payload(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "updated_at": self.updated_at,
            "profiles": [profile.to_storage_dict() for profile in self._profiles],
            "load_error": self.load_error or None,
        }


def identity_prompt_payload(
    profiles: Iterable[SupplementalIdentityProfile],
) -> list[dict[str, Any]]:
    return [profile.to_prompt_dict() for profile in profiles]


AuthoritativeIdentityProfile = SupplementalIdentityProfile
AuthoritativeIdentityStore = SupplementalIdentityStore
parse_authoritative_identity_profiles = parse_supplemental_identity_profiles


__all__ = [
    "SupplementalIdentityProfile",
    "SupplementalIdentityStore",
    "parse_supplemental_identity_profiles",
    # Compatibility aliases for existing imports and the legacy JSON filename.
    "AuthoritativeIdentityProfile",
    "AuthoritativeIdentityStore",
    "identity_prompt_payload",
    "parse_authoritative_identity_profiles",
]
