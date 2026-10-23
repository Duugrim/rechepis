function errorText(data, status) {
  if (!data) return `Ошибка ${status}`;
  const detail = data.detail ?? data.error;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  return detail || `Ошибка ${status}`;
}

export async function api(path, { method = "GET", body } = {}) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`/api${path}`, options);
  } catch {
    throw new Error("Нет связи с программой. Она запущена?");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) throw new Error(errorText(data, response.status));
  return data;
}

export const enc = encodeURIComponent;
