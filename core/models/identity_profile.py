"""Authoritative participant identity profiles used by memory-generation prompts."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class AuthoritativeIdentityProfile:
    """User-supplied identity facts; never inferred from conversation style."""

    user_id: str
    platform: str = ""
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    gender: str = ""
    pronouns: tuple[str, ...] = ()
    notes: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "AuthoritativeIdentityProfile":
        user_id = str(value.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("authoritative identity profile requires user_id")
        profile = cls(
            user_id=user_id,
            platform=str(value.get("platform") or "").strip(),
            display_name=str(value.get("display_name") or "").strip(),
            aliases=cls._strings(value.get("aliases")),
            gender=str(value.get("gender") or "").strip(),
            pronouns=cls._strings(value.get("pronouns")),
            notes=str(value.get("notes") or "").strip(),
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
        sender_id = str(sender_id or "").strip()
        if sender_id:
            return (
                sender_id.casefold() == self.user_id.casefold()
                and self._platform_matches(platform)
            )
        sender_name = str(sender_name or "").strip().casefold()
        return bool(
            sender_name
            and sender_name in {name.casefold() for name in self.names}
        )

    def matches_context(self, values: Iterable[Any]) -> bool:
        context = "\n".join(str(value or "") for value in values).casefold()
        if not context:
            return False
        user_id = self.user_id.casefold()
        if re.search(
            rf"(?<![a-z0-9]){re.escape(user_id)}(?![a-z0-9])",
            context,
        ):
            return True
        return any(name.casefold() in context for name in self.names)

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
                    f"authoritative identity {field_name} exceeds {limit} characters"
                )
        if len(self.aliases) > 20 or any(len(value) > 200 for value in self.aliases):
            raise ValueError("authoritative identity aliases exceed allowed limits")
        if len(self.pronouns) > 10 or any(
            len(value) > 100 for value in self.pronouns
        ):
            raise ValueError("authoritative identity pronouns exceed allowed limits")

    def _platform_matches(self, platform: str | None) -> bool:
        expected = self._normalize(self.platform)
        actual = self._normalize(platform)
        if not expected or not actual:
            return True
        return expected == actual or expected in actual or actual in expected

    @staticmethod
    def _normalize(value: Any) -> str:
        return "".join(
            character
            for character in str(value or "").casefold()
            if character.isalnum()
        )


def parse_authoritative_identity_profiles(
    raw: Any,
) -> list[AuthoritativeIdentityProfile]:
    """Validate serialized or in-memory profile records."""
    if isinstance(raw, dict):
        raw = raw.get("profiles", [])
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    if not isinstance(raw, list):
        raise ValueError("authoritative identity profiles must be an array")
    profiles = [
        AuthoritativeIdentityProfile.from_mapping(item)
        for item in raw
        if isinstance(item, dict)
    ]
    if len(profiles) != len(raw):
        raise ValueError("each authoritative identity profile must be a JSON object")
    for index, profile in enumerate(profiles):
        for other in profiles[:index]:
            if profile.user_id.casefold() != other.user_id.casefold():
                continue
            if profile._platform_matches(other.platform):
                raise ValueError(
                    "authoritative identity profiles contain overlapping "
                    "platform/user_id entries"
                )
    return profiles


class AuthoritativeIdentityStore:
    """Small atomic JSON store shared by WebUI and generation pipelines."""

    VERSION = 1

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        profiles: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._profiles: tuple[AuthoritativeIdentityProfile, ...] = ()
        self.updated_at: float | None = None
        self.load_error = ""
        if profiles is not None:
            self.replace_profiles(list(profiles), persist=False)
        elif self.path is not None:
            self.load()

    @property
    def profiles(self) -> list[AuthoritativeIdentityProfile]:
        return list(self._profiles)

    def load(self) -> list[AuthoritativeIdentityProfile]:
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
            profiles = parse_authoritative_identity_profiles(raw_profiles)
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
    ) -> list[AuthoritativeIdentityProfile]:
        if len(raw_profiles) > 200:
            raise ValueError("authoritative identity profiles cannot exceed 200 items")
        profiles = parse_authoritative_identity_profiles(raw_profiles)
        updated_at = time.time()
        if persist and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": self.VERSION,
                "updated_at": updated_at,
                "profiles": identity_prompt_payload(profiles),
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
            "profiles": identity_prompt_payload(self._profiles),
            "load_error": self.load_error or None,
        }


def identity_prompt_payload(
    profiles: Iterable[AuthoritativeIdentityProfile],
) -> list[dict[str, Any]]:
    return [profile.to_prompt_dict() for profile in profiles]


__all__ = [
    "AuthoritativeIdentityProfile",
    "AuthoritativeIdentityStore",
    "identity_prompt_payload",
    "parse_authoritative_identity_profiles",
]
