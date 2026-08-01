function inputValue(input, definition, fallbackValue) {
  if (!input) {
    return fallbackValue !== undefined ? fallbackValue : definition?.default;
  }
  if (definition?.type === "bool") return input.checked;
  if (definition?.type === "int") return Number.parseInt(input.value, 10);
  if (definition?.type === "float") return Number.parseFloat(input.value);
  return input.value;
}

function conditionMatches(condition, readValue) {
  if (!condition) return true;
  if (Array.isArray(condition.all)) {
    return condition.all.every(item => conditionMatches(item, readValue));
  }
  if (Array.isArray(condition.any)) {
    return condition.any.some(item => conditionMatches(item, readValue));
  }
  const value = readValue(condition.key);
  if (Object.prototype.hasOwnProperty.call(condition, "equals")) {
    return value === condition.equals;
  }
  if (Object.prototype.hasOwnProperty.call(condition, "not_equals")) {
    return value !== condition.not_equals;
  }
  if (Object.prototype.hasOwnProperty.call(condition, "greater_than")) {
    return Number(value) > Number(condition.greater_than);
  }
  return Boolean(value);
}

export function bindSettingDependencies({
  root,
  definitions,
  effectiveValues = {},
  inputSelector,
  rowSelector,
  sectionSelector,
}) {
  if (!root) return () => {};
  const inputFor = key => root.querySelector(`${inputSelector}[data-setting-key="${CSS.escape(key)}"]`);
  const readValue = key => inputValue(
    inputFor(key),
    definitions[key],
    effectiveValues[key],
  );
  const refresh = () => {
    root.querySelectorAll(rowSelector).forEach(row => {
      const key = row.dataset.settingsRow || row.dataset.timelineSettingRow;
      const visible = conditionMatches(definitions[key]?.visible_when, readValue);
      row.classList.toggle("hidden", !visible);
    });
    root.querySelectorAll(sectionSelector).forEach(section => {
      const rows = Array.from(section.querySelectorAll(rowSelector));
      section.classList.toggle("hidden", rows.length > 0 && rows.every(row => row.classList.contains("hidden")));
    });
  };
  root.querySelectorAll(inputSelector).forEach(input => {
    input.addEventListener("change", refresh);
    input.addEventListener("input", refresh);
  });
  refresh();
  return refresh;
}
