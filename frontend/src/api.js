const API_ROOT = '/api/v1'

export class ApiError extends Error {
  constructor(message, detail = null) {
    super(message)
    this.detail = detail
  }
}

export function adminHeaders() {
  const token = localStorage.getItem('cdk_loader_admin_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export async function request(path, options = {}) {
  const response = await fetch(`${API_ROOT}${path}`, options)
  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('application/json') ? await response.json() : await response.text()
  if (!response.ok) {
    const detail = payload?.detail ?? payload
    const message = typeof detail === 'string' ? detail : detail?.message || '请求失败'
    throw new ApiError(message, detail)
  }
  return payload
}

export async function requestWithMeta(path, options = {}) {
  const response = await fetch(`${API_ROOT}${path}`, options)
  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('application/json') ? await response.json() : await response.text()
  if (!response.ok) {
    const detail = payload?.detail ?? payload
    const message = typeof detail === 'string' ? detail : detail?.message || '请求失败'
    throw new ApiError(message, detail)
  }
  return { payload, response }
}

export function saveDownload(blob, filename) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename || 'accounts.json'
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function download(path, options = {}) {
  const response = await fetch(`${API_ROOT}${path}`, options)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new ApiError(payload.detail || '下载失败')
  }
  const disposition = response.headers.get('content-disposition') || ''
  const match = disposition.match(/filename="?([^";]+)"?/i)
  saveDownload(await response.blob(), match?.[1] || 'accounts.json')
}

export function triggerDownload(path) {
  const link = document.createElement('a')
  link.href = path.startsWith('/api/') ? path : `${API_ROOT}${path}`
  link.download = ''
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()
}
