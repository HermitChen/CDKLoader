<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  Activity,
  Archive,
  ChevronLeft,
  ChevronRight,
  Clipboard,
  CloudDownload,
  Dices,
  Download,
  FileUp,
  Gauge,
  KeyRound,
  LayoutDashboard,
  ListChecks,
  LoaderCircle,
  Mail,
  Network,
  LockKeyhole,
  LogOut,
  PackageOpen,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  Search,
  ScrollText,
  ShieldCheck,
  Trash2,
  Upload,
  Users,
  X,
} from 'lucide-vue-next'
import {
  Button as NButton,
  CalloutBox,
  Checkbox as NCheckbox,
  CodeBlock,
  ConfirmDialog,
  FilterSelect,
  FormField,
  FormSection,
  Input as NInput,
  ModalShell,
  StatCard,
  StatusPill,
  SelectMenu,
  TableShell,
  Toast,
  ToolbarShell,
} from 'nanocat-ui'
import { adminHeaders, ApiError, download, request, requestWithMeta, triggerDownload } from './api'

const isAdmin = ref(window.location.pathname.startsWith('/admin'))
const adminToken = ref(localStorage.getItem('cdk_loader_admin_token') || '')
const loginPassword = ref('')
const loginError = ref('')
const activeView = ref('overview')
const loading = ref(false)
const accountExportBusy = ref(false)
const accountValidationBusy = ref(false)
const accountValidationTask = ref(null)
const operationTasks = ref([])
const operationTaskTotal = ref(0)
const operationLogs = ref([])
const operationLogTotal = ref(0)
const operationTaskPage = ref(1)
const operationLogPage = ref(1)
const operationTaskPageSize = ref(15)
const operationLogPageSize = ref(30)
const operationTaskFilters = ref({ task_type: '', status: '' })
const operationLogFilters = ref({ q: '', operation_type: '', outcome: '' })
const operationTaskDetail = ref(null)
const operationTaskDetailOpen = ref(false)
const operationTaskDetailLoading = ref(false)
const operationTaskDetailError = ref('')
let adminTaskPollTimer = null
let adminTaskMessageTimer = null
let settingsMessageTimer = null
let adminTaskRefreshBusy = false
let operationViewPollTimer = null
let operationViewRefreshBusy = false
let operationTaskDetailPollTimer = null
let operationTaskDetailRefreshBusy = false
let operationTaskDetailId = ''
let operationTaskDetailAbortController = null
let proxyHealthPollTimer = null
let proxyHealthPollBusy = false
const pageSizeOptions = [
  { label: '15 / 页', value: 15 }, { label: '30 / 页', value: 30 },
  { label: '50 / 页', value: 50 }, { label: '100 / 页', value: 100 },
  { label: '显示全部', value: 0 },
]

const dashboard = ref(null)
const accounts = ref([])
const accountTotal = ref(0)
const cdks = ref([])
const cdkTotal = ref(0)
const redemptions = ref([])
const redemptionTotal = ref(0)
const accountPage = ref(1)
const cdkPage = ref(1)
const redemptionPage = ref(1)
const accountPageSize = ref(15)
const cdkPageSize = ref(15)
const redemptionPageSize = ref(15)
const accountFilters = ref({ q: '', status: '', has_refresh_token: '', email_type: '', cdk_id: '', redemption_id: '', relation_label: '' })
const cdkFilters = ref({ q: '', quota: '', email_type: '', status: '' })
const redemptionFilters = ref({ q: '', status: '', today: false })
const selectedAccountIds = ref([])
const selectedCdkIds = ref([])
const selectedRedemptionIds = ref([])
const selectionBusy = ref('')
const randomAccountCount = ref('')
const accountMessage = ref('')
const redemptionMessage = ref('')
const toasts = ref([])
const deleteDialog = ref({ open: false, kind: '', count: 0, label: '' })
const reissueDialog = ref({ open: false, id: '', prefix: '' })
const accountImportOpen = ref(false)
const cdkGeneratorOpen = ref(false)
const systemSettings = ref([])
const settingsSaving = ref(false)
const settingsMessage = ref('')
const settingsError = ref('')
const proxyGroups = ref([])
const proxyHealthSettings = ref({ enabled: true, interval_minutes: 5, threshold_ms: 1000 })
const proxyHealthSettingsSaved = ref({ enabled: true, interval_minutes: 5, threshold_ms: 1000 })
const proxyHealthSaving = ref(false)
const proxyHealthTestingKey = ref('')
const proxyHealthTestResults = ref({})
const proxyGroupOpen = ref(false)
const proxyGroupSaving = ref(false)
const proxyGroupError = ref('')
const proxyGroupBatchOpen = ref(false)
const proxyGroupBatchText = ref('')
const proxyGroupBatchError = ref('')
const proxyGroupBatchMessage = ref('')
const proxyGroupDeleteDialog = ref({ open: false, id: '', name: '' })
const proxyGroupForm = ref({
  id: '', name: '', strategy: 'request_random', rotation_interval_minutes: 0, enabled: true,
  nodes: [{ id: '', name: '节点 1', url: '', enabled: true, has_credentials: false }],
})

const importFiles = ref([])
const importInput = ref(null)
const importPreview = ref(null)
const importBusy = ref(false)
const importMessage = ref('')
const importOptions = ref({ duplicate_strategy: 'skip', prevalidate: true })

const cdkForm = ref({ count: 1, quota: 1, email_type: 'generic', export_format: 'json' })
const generatedCodes = ref([])
const cdkMessage = ref('')
const cdkFilterMessage = ref('')
const transientMessageTimers = new Map()

const codeInput = ref('')
const redeemBusy = ref(false)
const redeemError = ref('')
const redeemState = ref(null)
const redeemStage = ref('')
const exportTask = ref(null)
let exportSocket = null

const navItems = [
  { id: 'overview', label: '概览', icon: LayoutDashboard },
  { id: 'accounts', label: '账号池', icon: Users },
  { id: 'cdks', label: 'CDK', icon: KeyRound },
  { id: 'redemptions', label: '兑换记录', icon: Archive },
  { id: 'logs', label: '任务日志', icon: ScrollText },
  { id: 'proxies', label: '代理配置', icon: Network },
  { id: 'settings', label: '系统参数', icon: Settings2 },
]

const accountStatusOptions = [
  { label: '全部状态', value: '' }, { label: '可用', value: 'available' },
  { label: '待验活', value: 'pending_validation' }, { label: '已隔离', value: 'quarantined' },
  { label: '已失效', value: 'expired' }, { label: '已封禁', value: 'banned' },
  { label: '已预约', value: 'reserved' }, { label: '已交付', value: 'delivered' },
]
const refreshTokenOptions = [
  { label: 'RT：全部', value: '' },
  { label: '有 RT', value: 'true' },
  { label: '无 RT', value: 'false' },
]
const accountEmailTypeOptions = [
  { label: '全部邮箱', value: '' },
  { label: '通用邮箱', value: 'generic' },
  { label: '微软邮箱', value: 'ms' },
  { label: '苹果邮箱', value: 'icloud' },
  { label: '谷歌邮箱', value: 'gmail' },
]
const cdkStatusOptions = [
  { label: '全部状态', value: '' }, { label: '未使用', value: 'unused' },
  { label: '部分使用', value: 'partial' }, { label: '已耗尽', value: 'exhausted' },
  { label: '已过期', value: 'expired' }, { label: '已禁用', value: 'disabled' },
]
const cdkEmailTypeOptions = [
  { label: '通用邮箱', value: 'generic' },
  { label: '微软邮箱', value: 'ms' },
  { label: '苹果邮箱', value: 'icloud' },
  { label: '谷歌邮箱', value: 'gmail' },
]
const cdkFilterEmailTypeOptions = [
  { label: '全部邮箱', value: '' },
  ...cdkEmailTypeOptions,
]
const redemptionStatusOptions = [
  { label: '全部状态', value: '' }, { label: '排队中', value: 'queued' },
  { label: '处理中', value: 'processing' }, { label: '已完成', value: 'completed' },
  { label: '失败', value: 'failed' },
]

const pageMeta = {
  overview: { title: '概览', description: '账号库存、CDK 额度和今日兑换状态。' },
  accounts: { title: '账号池', description: '筛选账号库存，核对关联 CDK 并支持二次导出。' },
  cdks: { title: 'CDK', description: '管理提取码、额度、有效期和已兑换账号。' },
  redemptions: { title: '兑换记录', description: '查看完整 CDK、交付状态和关联账号。' },
  logs: { title: '任务日志', description: '查看验活、导出和导入任务的进度与结果。' },
  proxies: { title: '代理配置', description: '维护具名代理组和节点，供验活出口选择。' },
  settings: { title: '系统参数', description: '调整运行参数和自动清理策略，修改后立即生效。' },
}

const publicCodes = computed(() => codeInput.value.split(/[\n,]+/).map((value) => value.trim()).filter(Boolean))
const importTotalSize = computed(() => importFiles.value.reduce((total, file) => total + file.size, 0))
const activePageMeta = computed(() => pageMeta[activeView.value])
const publicStatus = computed(() => redeemState.value?.status || '')
const publicStatusText = computed(() => ({
  queued: '已创建任务',
  processing: '正在验活并补位',
  completed: '交付已完成',
  failed: '任务未完成',
  redelivery_ready: '已兑换，补发已就绪',
  redelivery_completed: '已兑换，补发完成',
}[publicStatus.value] || ''))
const selectedAccountsOnPage = computed(() => accounts.value.filter((item) => selectedAccountIds.value.includes(item.id)).length)
const selectedCdksOnPage = computed(() => cdks.value.filter((item) => selectedCdkIds.value.includes(item.id)).length)
const selectedRedemptionsOnPage = computed(() => redemptions.value.filter((item) => selectedRedemptionIds.value.includes(item.id)).length)
const allAccountsSelected = computed(() => accounts.value.length > 0 && selectedAccountsOnPage.value === accounts.value.length)
const allCdksSelected = computed(() => cdks.value.length > 0 && selectedCdksOnPage.value === cdks.value.length)
const allRedemptionsSelected = computed(() => redemptions.value.length > 0 && selectedRedemptionsOnPage.value === redemptions.value.length)
const allAccountResultsSelected = computed(() => accountTotal.value > 0 && selectedAccountIds.value.length === accountTotal.value)
const allCdkResultsSelected = computed(() => cdkTotal.value > 0 && selectedCdkIds.value.length === cdkTotal.value)
const allRedemptionResultsSelected = computed(() => redemptionTotal.value > 0 && selectedRedemptionIds.value.length === redemptionTotal.value)

const maxBulkSelection = 5000

function statusTone(value) {
  if (['available', 'completed', 'unused', 'validating', 'redelivery_ready', 'redelivery_completed', 'valid'].includes(value)) return 'success'
  if (['reserved', 'partial', 'processing', 'running', 'queued', 'pending_validation', 'started', 'inconclusive', 'skipped'].includes(value)) return 'warning'
  if (['expired', 'banned', 'failed', 'exhausted', 'disabled', 'invalid'].includes(value)) return 'error'
  return 'neutral'
}

function statusLabel(value) {
  return {
    available: '可用', reserved: '已预约', delivered: '已交付', pending_validation: '待验活',
    quarantined: '已隔离', expired: '已失效', banned: '已封禁', completed: '已完成',
    processing: '处理中', running: '处理中', queued: '排队中', failed: '失败', unused: '未使用', partial: '部分使用',
    exhausted: '已耗尽', disabled: '已禁用', validating: '验活中',
  }[value] || value
}

function taskTypeLabel(value) {
  return {
    manual_validation: '手动验活',
    account_import_validation: '导入预验活',
    redemption_export: '兑换导出',
    redelivery_export: '补发导出',
    account_import: '账号导入',
    account_export: '账号导出',
    redemption: '兑换',
  }[value] || value
}

function outcomeLabel(value) {
  return {
    queued: '已排队', started: '执行中', completed: '已完成', failed: '失败',
    valid: '有效', invalid: '无效', inconclusive: '待确认', skipped: '已跳过',
    processing: '处理中', redelivery_ready: '补发就绪',
  }[value] || value
}

function formatDate(value, format = 'datetime') {
  if (!value) return '—'
  const options = format === 'date'
    ? { dateStyle: 'medium', timeZone: 'Asia/Shanghai' }
    : { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Shanghai' }
  return new Intl.DateTimeFormat('zh-CN', options).format(new Date(value))
}

function cdkLabel(cdk) {
  if (!cdk) return '—'
  return cdk.code || `${cdk.prefix}-****`
}

function emailTypeLabel(value) {
  return {
    generic: '通用邮箱',
    ms: '微软邮箱',
    icloud: '苹果邮箱',
    gmail: '谷歌邮箱',
  }[value] || '通用邮箱'
}

function messageTone(message) {
  if (/(失败|错误|不可|超时|错误)/.test(message || '')) return 'error'
  if (/(保留|跳过|历史)/.test(message || '')) return 'warning'
  return 'success'
}

function pushToast(type, title, message) {
  const id = `${Date.now()}-${Math.random()}`
  toasts.value = [...toasts.value, { id, type, title, message }]
  window.setTimeout(() => removeToast(id), 4200)
}

function removeToast(id) {
  toasts.value = toasts.value.filter((item) => item.id !== id)
}

async function adminRequest(path, options = {}) {
  return request(path, { ...options, headers: { ...adminHeaders(), ...(options.headers || {}) } })
}

async function adminRequestWithMeta(path, options = {}) {
  return requestWithMeta(path, { ...options, headers: { ...adminHeaders(), ...(options.headers || {}) } })
}

async function login() {
  loading.value = true
  loginError.value = ''
  try {
    const result = await request('/admin/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: loginPassword.value }),
    })
    adminToken.value = result.token
    localStorage.setItem('cdk_loader_admin_token', result.token)
    loginPassword.value = ''
    await loadAdminData()
  } catch (error) {
    loginError.value = error.message
  } finally {
    loading.value = false
  }
}

function logout() {
  localStorage.removeItem('cdk_loader_admin_token')
  adminToken.value = ''
  window.location.assign('/')
}

async function loadAdminData() {
  if (!adminToken.value) return
  try {
    await Promise.all([
      loadDashboard(),
      loadAccounts(),
      loadCdks(),
      loadRedemptions(),
      loadOperationTasks(),
      loadOperationLogs(),
      loadProxyGroups(),
      loadProxyHealthSettings(),
      loadSettings(),
    ])
  } catch (error) {
    if (error instanceof ApiError && /认证/.test(error.message)) logout()
  }
}

async function loadDashboard() {
  dashboard.value = await adminRequest('/admin/dashboard')
}

async function loadSettings() {
  const result = await adminRequest('/admin/settings')
  systemSettings.value = result.groups || []
  settingsError.value = ''
}

async function loadProxyGroups() {
  const result = await adminRequest('/admin/proxy-groups')
  proxyGroups.value = result.groups || []
}

async function loadProxyHealthSettings() {
  const result = await adminRequest('/admin/proxy-health/settings')
  proxyHealthSettings.value = {
    enabled: result.enabled !== false,
    interval_minutes: Number(result.interval_minutes) || 5,
    threshold_ms: Number(result.threshold_ms) || 1000,
  }
  proxyHealthSettingsSaved.value = { ...proxyHealthSettings.value }
}

const proxyHealthSettingsDirty = computed(() => (
  proxyHealthSettings.value.enabled !== proxyHealthSettingsSaved.value.enabled
  || Number(proxyHealthSettings.value.interval_minutes) !== proxyHealthSettingsSaved.value.interval_minutes
  || Number(proxyHealthSettings.value.threshold_ms) !== proxyHealthSettingsSaved.value.threshold_ms
))

async function saveProxyHealthSettings() {
  proxyHealthSaving.value = true
  try {
    const result = await adminRequest('/admin/proxy-health/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled: proxyHealthSettings.value.enabled,
        interval_minutes: Number(proxyHealthSettings.value.interval_minutes),
        threshold_ms: Number(proxyHealthSettings.value.threshold_ms),
      }),
    })
    proxyHealthSettingsSaved.value = {
      enabled: result.enabled,
      interval_minutes: result.interval_minutes,
      threshold_ms: result.threshold_ms,
    }
    proxyHealthSettings.value = { ...proxyHealthSettingsSaved.value }
    pushToast('success', '检测设置已保存', '代理健康检测参数已立即生效。')
    await loadProxyGroups()
  } catch (error) {
    pushToast('error', '保存检测设置失败', error.message)
  } finally {
    proxyHealthSaving.value = false
  }
}

function proxyHealthTestKey(groupId, nodeId) {
  return `${groupId}:${nodeId}`
}

function proxyNodeHealthLabel(node) {
  if (!node.enabled) return '节点已停用'
  if (node.health?.blocked && node.health?.eligible === false) return '已隔离'
  if (node.health?.blocked) return '隔离已停用'
  if (node.health?.state === 'healthy') return '可用'
  if (node.health?.error?.startsWith('probe_target_http_')) return '目标响应异常'
  if (node.health?.error === 'probe_internal_error') return '检测异常'
  return '待检测'
}

function proxyNodeHealthTone(node) {
  if (node.health?.blocked && node.health?.eligible === false) return 'error'
  if (node.health?.state === 'healthy' && node.health?.eligible !== false) return 'success'
  if (!node.enabled) return 'neutral'
  return 'info'
}

function proxyNodeHealthSummary(node, groupId) {
  const testKey = proxyHealthTestKey(groupId, node.id)
  if (proxyHealthTestingKey.value === testKey) return '手动测活中…'
  const manual = proxyHealthTestResults.value[testKey]
  if (manual) {
    return manual.ok
      ? `手测连通 ${manual.latency_ms} ms · HTTP ${manual.status || '—'}`
      : `手测失败 · ${manual.error || '连接失败'}`
  }
  const health = node.health || {}
  if (health.checked_at) {
    const latency = health.latency_ms === null ? '' : `${health.latency_ms} ms · `
    const errors = {
      proxy_connection_timeout: '自动探测超时',
      proxy_connection_failed: '自动探测连接失败',
      proxy_dns_failed: '代理域名解析失败',
      proxy_target_dns_failed: '目标域名解析失败',
      proxy_authentication_failed: '代理认证失败',
      proxy_socks_handshake_failed: 'SOCKS 握手或隧道失败',
      proxy_tls_verification_failed: 'TLS 证书校验失败',
      proxy_tls_handshake_failed: 'TLS 握手失败',
      proxy_configuration_invalid: '代理配置无效',
      probe_internal_error: '检测客户端异常，本次未判定',
      latency_exceeded: '耗时超阈值',
    }
    if (health.error?.startsWith('probe_target_http_')) {
      const status = health.error.split('_').pop()
      const recovery = health.blocked ? `未计入恢复 ${health.recovery_successes || 0}/2` : '节点资格保留'
      return `${latency}目标 HTTP ${status} · ${recovery}`
    }
    if (health.blocked && health.eligible === false) {
      return `${latency}${errors[health.error] || '恢复观察'} · 复测 ${health.recovery_successes || 0}/2`
    }
    if (health.state === 'healthy') return `${latency}自动检测 · ${formatDate(health.checked_at)}`
    if (health.blocked) return `${latency}隔离策略已停用`
    if (errors[health.error]) return `${latency}${errors[health.error]}`
  }
  return '等待自动检测'
}

async function testProxyNode(group, node) {
  if (proxyHealthTestingKey.value) return
  const testKey = proxyHealthTestKey(group.id, node.id)
  proxyHealthTestingKey.value = testKey
  const remaining = { ...proxyHealthTestResults.value }
  delete remaining[testKey]
  proxyHealthTestResults.value = remaining
  try {
    const response = await adminRequest(`/admin/proxy-groups/${group.id}/nodes/${node.id}/test`, { method: 'POST' })
    proxyHealthTestResults.value = { ...proxyHealthTestResults.value, [testKey]: response.result }
    if (response.result.ok) {
      pushToast('success', '代理连通正常', `${response.result.latency_ms} ms · HTTP ${response.result.status}`)
    } else {
      pushToast('warning', '代理测活未通过', response.result.error || '连接失败')
    }
  } catch (error) {
    pushToast('error', '代理测活失败', error.message)
  } finally {
    proxyHealthTestingKey.value = ''
  }
}

function syncProxyHealthPolling() {
  if (proxyHealthPollTimer) window.clearInterval(proxyHealthPollTimer)
  proxyHealthPollTimer = null
  if (!isAdmin.value || !adminToken.value || activeView.value !== 'proxies' || document.hidden) return
  proxyHealthPollTimer = window.setInterval(async () => {
    if (proxyHealthPollBusy || document.hidden) return
    proxyHealthPollBusy = true
    try {
      await loadProxyGroups()
    } catch (error) {
      if (error instanceof ApiError && /认证/.test(error.message)) logout()
    } finally {
      proxyHealthPollBusy = false
    }
  }, 30000)
}

function systemSettingItem(key) {
  for (const group of systemSettings.value) {
    const item = group.items?.find((candidate) => candidate.key === key)
    if (item) return item
  }
  return null
}

function validationEgressMode() {
  return String(systemSettingItem('validation_egress_mode')?.value || 'direct').trim().toLowerCase()
}

function isSystemSettingVisible(item) {
  const mode = validationEgressMode()
  if (item.key === 'validation_proxy_group_id') return mode === 'group'
  if (item.key === 'validation_proxy') return mode === 'account'
  return true
}

function updateSystemSettingValue(item, value) {
  item.value = value
  if (item.key === 'validation_egress_mode' && value !== 'group') {
    const groupItem = systemSettingItem('validation_proxy_group_id')
    if (groupItem) groupItem.value = ''
  }
  if (item.key === 'validation_proxy_group_id' && value) {
    const modeItem = systemSettingItem('validation_egress_mode')
    if (modeItem) modeItem.value = 'group'
  }
}

function resetProxyGroupForm(group = null) {
  proxyGroupBatchOpen.value = false
  proxyGroupBatchText.value = ''
  proxyGroupBatchError.value = ''
  proxyGroupBatchMessage.value = ''
  if (group) {
    proxyGroupForm.value = {
      id: group.id,
      name: group.name,
      strategy: group.strategy,
      rotation_interval_minutes: group.rotation_interval_minutes || 0,
      enabled: group.enabled !== false,
      nodes: (group.nodes || []).map((node, index) => ({
        id: node.id,
        name: node.name || `节点 ${index + 1}`,
        url: node.url || '',
        enabled: node.enabled !== false,
        has_credentials: node.has_credentials === true,
      })),
    }
  } else {
    proxyGroupForm.value = {
      id: '', name: '', strategy: 'request_random', rotation_interval_minutes: 0, enabled: true,
      nodes: [{ id: '', name: '节点 1', url: '', enabled: true, has_credentials: false }],
    }
  }
  proxyGroupError.value = ''
  proxyGroupOpen.value = true
}

function openProxyGroupBatch() {
  proxyGroupBatchOpen.value = true
  proxyGroupBatchError.value = ''
  proxyGroupBatchMessage.value = ''
}

function closeProxyGroupBatch() {
  proxyGroupBatchOpen.value = false
  proxyGroupBatchText.value = ''
  proxyGroupBatchError.value = ''
  proxyGroupBatchMessage.value = ''
}

function proxyUrlKey(value) {
  const raw = String(value || '').trim()
  try {
    const parsed = new URL(raw)
    return [
      parsed.protocol.toLowerCase(),
      parsed.username,
      parsed.password,
      parsed.hostname.toLowerCase(),
      parsed.port,
    ].join('|')
  } catch {
    return raw
  }
}

function isProxyUrl(value) {
  const raw = String(value || '').trim()
  if (!raw || /\s/.test(raw)) return false
  try {
    const parsed = new URL(raw)
    if (!['http:', 'https:', 'socks4:', 'socks4a:', 'socks5:', 'socks5h:'].includes(parsed.protocol.toLowerCase())) return false
    if (!parsed.hostname || (parsed.pathname && parsed.pathname !== '/') || parsed.search || parsed.hash) return false
    return true
  } catch {
    return false
  }
}

function appendProxyGroupBatch() {
  const lines = proxyGroupBatchText.value.split(/\r?\n/)
  if (!proxyGroupBatchText.value.trim()) {
    proxyGroupBatchError.value = '请粘贴代理地址，每行一个。'
    proxyGroupBatchMessage.value = ''
    return
  }
  const form = proxyGroupForm.value
  const hasBlankPlaceholder = form.nodes.length === 1 && !form.nodes[0].url.trim()
  const existingKeys = new Set(form.nodes.filter((node) => node.url.trim()).map((node) => proxyUrlKey(node.url)))
  const seenKeys = new Set()
  const additions = []
  const invalid = []
  let duplicateCount = 0

  for (const [index, source] of lines.entries()) {
    const raw = source.trim()
    if (!raw || raw.startsWith('#')) continue
    const parts = raw.split(/\s+/)
    const url = parts.shift() || ''
    if (!isProxyUrl(url)) {
      invalid.push({ line: index + 1, raw })
      continue
    }
    const key = proxyUrlKey(url)
    if (existingKeys.has(key) || seenKeys.has(key)) {
      duplicateCount += 1
      continue
    }
    if (form.nodes.length - (hasBlankPlaceholder ? 1 : 0) + additions.length >= 256) {
      invalid.push({ line: index + 1, raw, limit: true })
      continue
    }
    seenKeys.add(key)
    additions.push({
      id: '',
      name: parts.join(' ').trim() || `节点 ${form.nodes.length - (hasBlankPlaceholder ? 1 : 0) + additions.length + 1}`,
      url,
      enabled: true,
      has_credentials: false,
    })
  }

  if (additions.length) {
    if (hasBlankPlaceholder) form.nodes.splice(0, 1)
    form.nodes.push(...additions)
  }
  proxyGroupBatchMessage.value = additions.length || duplicateCount
    ? `已添加 ${additions.length} 个节点${duplicateCount ? `，跳过 ${duplicateCount} 个重复地址` : ''}。`
    : ''
  proxyGroupBatchError.value = invalid.length
    ? `${invalid.length} 行未添加：${invalid.slice(0, 5).map((item) => `${item.line} 行${item.limit ? '超过 256 个节点上限' : '地址格式无效'}`).join('、')}${invalid.length > 5 ? '等' : ''}。`
    : ''
  proxyGroupBatchText.value = invalid.map((item) => item.raw).join('\n')
  if (!additions.length && !duplicateCount && !invalid.length) {
    proxyGroupBatchError.value = '没有可添加的代理地址。'
    return
  }
  if (!invalid.length) proxyGroupBatchOpen.value = false
}

function addProxyGroupNode() {
  proxyGroupForm.value.nodes.push({
    id: '',
    name: `节点 ${proxyGroupForm.value.nodes.length + 1}`,
    url: '',
    enabled: true,
    has_credentials: false,
  })
}

function removeProxyGroupNode(index) {
  if (proxyGroupForm.value.nodes.length <= 1) return
  proxyGroupForm.value.nodes.splice(index, 1)
}

async function saveProxyGroup() {
  const form = proxyGroupForm.value
  if (!form.name.trim()) {
    proxyGroupError.value = '请输入代理组名称。'
    return
  }
  if (!form.nodes.length || form.nodes.some((node) => !node.url.trim())) {
    proxyGroupError.value = '至少保留一个代理节点，并填写节点地址。'
    return
  }
  proxyGroupSaving.value = true
  proxyGroupError.value = ''
  try {
    const path = form.id ? `/admin/proxy-groups/${form.id}` : '/admin/proxy-groups'
    const result = await adminRequest(path, {
      method: form.id ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: form.name,
        strategy: form.strategy,
        rotation_interval_minutes: Number(form.rotation_interval_minutes) || 0,
        enabled: form.enabled,
        nodes: form.nodes.map((node) => ({
          id: node.id || undefined,
          name: node.name,
          url: node.url,
          enabled: node.enabled,
        })),
      }),
    })
    if (result.group) {
      const index = proxyGroups.value.findIndex((item) => item.id === result.group.id)
      if (index >= 0) proxyGroups.value[index] = result.group
      else proxyGroups.value.push(result.group)
    }
    proxyGroupOpen.value = false
    await loadSettings()
    pushToast('success', '代理组已保存', '代理组配置已立即生效。')
  } catch (error) {
    proxyGroupError.value = error.message
    pushToast('error', '保存代理组失败', error.message)
  } finally {
    proxyGroupSaving.value = false
  }
}

function openDeleteProxyGroup(group) {
  if (group.can_delete === false) {
    pushToast('warning', '代理组正在使用', '请先在系统设置中取消选择该代理组。')
    return
  }
  proxyGroupDeleteDialog.value = { open: true, id: group.id, name: group.name }
}

function closeDeleteProxyGroup() {
  proxyGroupDeleteDialog.value = { open: false, id: '', name: '' }
}

async function confirmDeleteProxyGroup() {
  const { id } = proxyGroupDeleteDialog.value
  closeDeleteProxyGroup()
  try {
    await adminRequest(`/admin/proxy-groups/${id}`, { method: 'DELETE' })
    proxyGroups.value = proxyGroups.value.filter((item) => item.id !== id)
    await loadSettings()
    pushToast('success', '代理组已删除', '代理组已从配置中移除。')
  } catch (error) {
    pushToast('error', '删除代理组失败', error.message)
    await loadProxyGroups()
  }
}

function normalizedPageSize(value) {
  return Number(value) || 0
}

function filterParams(filters) {
  const params = new URLSearchParams()
  if (filters.q.trim()) params.set('q', filters.q.trim())
  if (filters.quota?.trim()) params.set('quota', filters.quota.trim())
  if (filters.status) params.set('status', filters.status)
  if (filters.has_refresh_token) params.set('has_refresh_token', filters.has_refresh_token)
  if (filters.email_type) params.set('email_type', filters.email_type)
  if (filters.cdk_id) params.set('cdk_id', filters.cdk_id)
  if (filters.redemption_id) params.set('redemption_id', filters.redemption_id)
  if (filters.today) params.set('today', 'true')
  return params
}

function queryString(params) {
  const value = params.toString()
  return value ? `?${value}` : ''
}

function listQuery(filters, page, requestedPageSize) {
  const params = filterParams(filters)
  const size = normalizedPageSize(requestedPageSize)
  params.set('limit', String(size))
  params.set('offset', String(size ? (page - 1) * size : 0))
  return queryString(params)
}

function operationTaskQuery() {
  const params = new URLSearchParams()
  if (operationTaskFilters.value.task_type) params.set('task_type', operationTaskFilters.value.task_type)
  if (operationTaskFilters.value.status) params.set('status', operationTaskFilters.value.status)
  const size = normalizedPageSize(operationTaskPageSize.value)
  params.set('limit', String(size))
  params.set('offset', String(size ? (operationTaskPage.value - 1) * size : 0))
  return queryString(params)
}

function operationLogQuery() {
  const params = new URLSearchParams()
  if (operationLogFilters.value.q.trim()) params.set('q', operationLogFilters.value.q.trim())
  if (operationLogFilters.value.operation_type) params.set('operation_type', operationLogFilters.value.operation_type)
  if (operationLogFilters.value.outcome) params.set('outcome', operationLogFilters.value.outcome)
  const size = normalizedPageSize(operationLogPageSize.value)
  params.set('limit', String(size))
  params.set('offset', String(size ? (operationLogPage.value - 1) * size : 0))
  return queryString(params)
}

function pageCount(total, requestedPageSize) {
  const size = normalizedPageSize(requestedPageSize)
  return size ? Math.max(1, Math.ceil(total / size)) : 1
}

function pageRange(total, page, requestedPageSize) {
  if (!total) return '0 / 0'
  const size = normalizedPageSize(requestedPageSize)
  const start = size ? (page - 1) * size + 1 : 1
  const end = size ? Math.min(page * size, total) : total
  return `${start}-${end} / ${total}`
}

function searchAccounts() {
  accountPage.value = 1
  loadAccounts({ clearSelection: true })
}

function stopAdminTaskPolling() {
  if (adminTaskPollTimer) {
    window.clearInterval(adminTaskPollTimer)
    adminTaskPollTimer = null
  }
}

function clearAdminTaskMessageTimer() {
  if (adminTaskMessageTimer) {
    window.clearTimeout(adminTaskMessageTimer)
    adminTaskMessageTimer = null
  }
}

function scheduleAdminTaskMessageClear() {
  clearAdminTaskMessageTimer()
  adminTaskMessageTimer = window.setTimeout(() => {
    accountMessage.value = ''
    accountValidationTask.value = null
    adminTaskMessageTimer = null
  }, 5000)
}

function clearTransientMessageTimer(key) {
  const timer = transientMessageTimers.get(key)
  if (timer) window.clearTimeout(timer)
  transientMessageTimers.delete(key)
}

function watchTransientMessage(messageRef, key) {
  watch(messageRef, (message) => {
    clearTransientMessageTimer(key)
    if (!message) return
    transientMessageTimers.set(key, window.setTimeout(() => {
      if (messageRef.value === message) messageRef.value = ''
      transientMessageTimers.delete(key)
    }, 5000))
  })
}

watchTransientMessage(redemptionMessage, 'redemption')
watchTransientMessage(importMessage, 'import')
watchTransientMessage(cdkMessage, 'cdk')
watchTransientMessage(cdkFilterMessage, 'cdk-filter')

function clearSettingsMessageTimer() {
  if (settingsMessageTimer) {
    window.clearTimeout(settingsMessageTimer)
    settingsMessageTimer = null
  }
}

function scheduleSettingsMessageClear() {
  clearSettingsMessageTimer()
  settingsMessageTimer = window.setTimeout(() => {
    settingsMessage.value = ''
    settingsMessageTimer = null
  }, 5000)
}

async function refreshAdminTask(taskId) {
  if (!taskId || adminTaskRefreshBusy) return
  adminTaskRefreshBusy = true
  try {
    const task = await adminRequest(`/admin/operation-tasks/${taskId}`)
    accountValidationTask.value = task
    if (task.status === 'completed' || task.status === 'failed') {
      stopAdminTaskPolling()
      accountMessage.value = task.status === 'completed'
        ? `验活完成：${task.processed}/${task.total}，有效 ${task.valid_count}，无效 ${task.invalid_count}，待确认 ${task.inconclusive_count}，RT 恢复 ${task.recoverable_count}。`
        : `验活任务失败：${task.error_message || '请查看任务日志。'}`
      scheduleAdminTaskMessageClear()
      await Promise.all([loadAccounts(), loadOperationTasks(), loadOperationLogs()])
    } else {
      accountMessage.value = `验活进行中：${task.processed}/${task.total}（${task.percent}%）`
    }
  } catch (error) {
    stopAdminTaskPolling()
    pushToast('error', '读取验活进度失败', error.message)
  } finally {
    adminTaskRefreshBusy = false
  }
}

function trackAdminTask(taskId) {
  stopAdminTaskPolling()
  clearAdminTaskMessageTimer()
  if (!taskId) return
  refreshAdminTask(taskId)
  adminTaskPollTimer = window.setInterval(() => refreshAdminTask(taskId), 900)
}

async function exportSelectedAccounts() {
  await exportAccounts(selectedAccountIds.value, '导出完成')
}

async function validateSelectedAccounts() {
  const ids = [...selectedAccountIds.value]
  if (!ids.length || accountValidationBusy.value) return
  accountValidationBusy.value = true
  clearAdminTaskMessageTimer()
  accountMessage.value = ''
  try {
    const { payload: result, response } = await adminRequestWithMeta('/admin/accounts/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    })
    const taskId = response.headers.get('x-operation-task-id')
    accountValidationTask.value = taskId ? { id: taskId, status: 'queued', processed: 0, total: ids.length, percent: 0 } : null
    selectedAccountIds.value = []
    const accepted = result.accepted || 0
    const skipped = result.skipped?.length || 0
    accountMessage.value = accepted
      ? `已提交 ${accepted} 个账号验活${skipped ? `，跳过 ${skipped} 个不可验活账号` : ''}。`
      : `没有可验活的账号${skipped ? `，跳过 ${skipped} 个不可验活账号` : ''}。`
    pushToast(accepted ? 'success' : 'warning', '批量验活', accountMessage.value)
    await Promise.all([loadAccounts(), loadOperationTasks(), loadOperationLogs()])
    if (taskId) trackAdminTask(taskId)
  } catch (error) {
    accountMessage.value = error.message
    pushToast('error', '批量验活失败', error.message)
  } finally {
    accountValidationBusy.value = false
  }
}

async function reexportAccount(account) {
  await exportAccounts([account.id], '二次导出完成')
}

async function exportAccounts(ids, title) {
  const selectedCount = ids.length
  if (!selectedCount) return
  accountExportBusy.value = true
  try {
    await download('/admin/accounts/export', {
      method: 'POST',
      headers: { ...adminHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    })
    pushToast('success', title, `已导出 ${selectedCount} 个账号。`)
  } catch (error) {
    pushToast('error', '导出失败', error.message)
  } finally {
    accountExportBusy.value = false
  }
}

function searchCdks() {
  const quota = cdkFilters.value.quota.trim()
  if (quota && !/^\d+(?:\s*\/\s*\d+)?$/.test(quota)) {
    cdkFilterMessage.value = '额度请输入总额度（如 10）或剩余 / 总额度（如 0/10）。'
    return
  }
  cdkFilterMessage.value = ''
  cdkPage.value = 1
  loadCdks({ clearSelection: true })
}

function searchRedemptions() {
  redemptionPage.value = 1
  loadRedemptions({ clearSelection: true })
}

async function loadAccounts({ clearSelection = false } = {}) {
  const result = await adminRequest(`/admin/accounts${listQuery(accountFilters.value, accountPage.value, accountPageSize.value)}`)
  accounts.value = result.items
  accountTotal.value = result.total
  if (accountPage.value > pageCount(accountTotal.value, accountPageSize.value)) {
    accountPage.value = pageCount(accountTotal.value, accountPageSize.value)
    return loadAccounts({ clearSelection })
  }
  if (clearSelection) selectedAccountIds.value = []
}

async function loadCdks({ clearSelection = false } = {}) {
  const result = await adminRequest(`/admin/cdks${listQuery(cdkFilters.value, cdkPage.value, cdkPageSize.value)}`)
  cdks.value = result.items
  cdkTotal.value = result.total
  if (cdkPage.value > pageCount(cdkTotal.value, cdkPageSize.value)) {
    cdkPage.value = pageCount(cdkTotal.value, cdkPageSize.value)
    return loadCdks({ clearSelection })
  }
  if (clearSelection) selectedCdkIds.value = []
}

async function loadRedemptions({ clearSelection = false } = {}) {
  const result = await adminRequest(`/admin/redemptions${listQuery(redemptionFilters.value, redemptionPage.value, redemptionPageSize.value)}`)
  redemptions.value = result.items
  redemptionTotal.value = result.total
  if (redemptionPage.value > pageCount(redemptionTotal.value, redemptionPageSize.value)) {
    redemptionPage.value = pageCount(redemptionTotal.value, redemptionPageSize.value)
    return loadRedemptions({ clearSelection })
  }
  if (clearSelection) selectedRedemptionIds.value = []
}

async function loadOperationTasks() {
  const result = await adminRequest(`/admin/operation-tasks${operationTaskQuery()}`)
  operationTasks.value = result.items
  operationTaskTotal.value = result.total
  if (operationTaskPage.value > pageCount(operationTaskTotal.value, operationTaskPageSize.value)) {
    operationTaskPage.value = pageCount(operationTaskTotal.value, operationTaskPageSize.value)
    return loadOperationTasks()
  }
}

async function loadOperationLogs() {
  const result = await adminRequest(`/admin/operation-logs${operationLogQuery()}`)
  operationLogs.value = result.items
  operationLogTotal.value = result.total
  if (operationLogPage.value > pageCount(operationLogTotal.value, operationLogPageSize.value)) {
    operationLogPage.value = pageCount(operationLogTotal.value, operationLogPageSize.value)
    return loadOperationLogs()
  }
}

async function loadActiveView(view = activeView.value) {
  if (view === 'overview') return loadDashboard()
  if (view === 'accounts') return loadAccounts()
  if (view === 'cdks') return loadCdks()
  if (view === 'redemptions') return loadRedemptions()
  if (view === 'logs') return Promise.all([loadOperationTasks(), loadOperationLogs()])
  if (view === 'proxies') return Promise.all([loadProxyGroups(), loadProxyHealthSettings(), loadSettings()])
  if (view === 'settings') return loadSettings()
  return null
}

async function refreshActiveView(view = activeView.value) {
  if (!adminToken.value) return
  try {
    await loadActiveView(view)
  } catch (error) {
    if (error instanceof ApiError && /认证/.test(error.message)) {
      logout()
    } else {
      pushToast('error', '刷新页面数据失败', error.message)
    }
  }
}

function stopOperationViewPolling() {
  if (operationViewPollTimer) {
    window.clearInterval(operationViewPollTimer)
    operationViewPollTimer = null
  }
}

async function refreshOperationView() {
  if (activeView.value !== 'logs' || operationViewRefreshBusy) return
  operationViewRefreshBusy = true
  try {
    await loadActiveView('logs')
  } catch (error) {
    if (error instanceof ApiError && /认证/.test(error.message)) logout()
  } finally {
    operationViewRefreshBusy = false
  }
}

function syncOperationViewPolling(view) {
  stopOperationViewPolling()
  if (view === 'logs') {
    refreshOperationView()
    operationViewPollTimer = window.setInterval(refreshOperationView, 1500)
    return
  }
  refreshActiveView(view)
}

function stopOperationTaskDetailPolling({ cancelRequest = true } = {}) {
  if (operationTaskDetailPollTimer) {
    window.clearInterval(operationTaskDetailPollTimer)
    operationTaskDetailPollTimer = null
  }
  if (cancelRequest) {
    operationTaskDetailAbortController?.abort()
    operationTaskDetailAbortController = null
    operationTaskDetailRefreshBusy = false
  }
}

async function refreshOperationTaskDetail(taskId, { silent = false } = {}) {
  if (!taskId || operationTaskDetailRefreshBusy) return
  operationTaskDetailRefreshBusy = true
  if (!silent) {
    operationTaskDetailLoading.value = true
    operationTaskDetailError.value = ''
  }
  const abortController = new AbortController()
  operationTaskDetailAbortController = abortController
  try {
    const detail = await adminRequest(`/admin/operation-tasks/${taskId}`, { signal: abortController.signal })
    if (operationTaskDetailId !== taskId) return
    operationTaskDetail.value = detail
    operationTaskDetailError.value = ''
    if (detail.status === 'completed' || detail.status === 'failed') stopOperationTaskDetailPolling({ cancelRequest: false })
  } catch (error) {
    if (!silent && error.name !== 'AbortError' && operationTaskDetailId === taskId) operationTaskDetailError.value = error.message
  } finally {
    if (operationTaskDetailAbortController === abortController) {
      operationTaskDetailAbortController = null
      if (!silent) operationTaskDetailLoading.value = false
      operationTaskDetailRefreshBusy = false
    }
  }
}

function trackOperationTaskDetail(taskId) {
  stopOperationTaskDetailPolling()
  if (!taskId) return
  operationTaskDetailPollTimer = window.setInterval(() => {
    refreshOperationTaskDetail(taskId, { silent: true })
  }, 900)
}

async function openOperationTask(task) {
  stopOperationTaskDetailPolling()
  operationTaskDetailOpen.value = true
  operationTaskDetailId = task.id
  operationTaskDetail.value = null
  operationTaskDetailError.value = ''
  await refreshOperationTaskDetail(task.id)
  if (operationTaskDetail.value?.status === 'queued' || operationTaskDetail.value?.status === 'running') {
    trackOperationTaskDetail(task.id)
  }
}

function closeOperationTaskDetail() {
  stopOperationTaskDetailPolling()
  operationTaskDetailOpen.value = false
  operationTaskDetailId = ''
  operationTaskDetail.value = null
  operationTaskDetailLoading.value = false
  operationTaskDetailError.value = ''
}

function changeAccountPage(delta) {
  const nextPage = Math.min(pageCount(accountTotal.value, accountPageSize.value), Math.max(1, accountPage.value + delta))
  if (nextPage === accountPage.value) return
  accountPage.value = nextPage
  loadAccounts()
}

function changeCdkPage(delta) {
  const nextPage = Math.min(pageCount(cdkTotal.value, cdkPageSize.value), Math.max(1, cdkPage.value + delta))
  if (nextPage === cdkPage.value) return
  cdkPage.value = nextPage
  loadCdks()
}

function changeRedemptionPage(delta) {
  const nextPage = Math.min(pageCount(redemptionTotal.value, redemptionPageSize.value), Math.max(1, redemptionPage.value + delta))
  if (nextPage === redemptionPage.value) return
  redemptionPage.value = nextPage
  loadRedemptions()
}

function changeAccountPageSize() {
  accountPage.value = 1
  loadAccounts()
}

function changeCdkPageSize() {
  cdkPage.value = 1
  loadCdks()
}

function changeRedemptionPageSize() {
  redemptionPage.value = 1
  loadRedemptions()
}

function changeOperationTaskPage(delta) {
  const nextPage = Math.min(pageCount(operationTaskTotal.value, operationTaskPageSize.value), Math.max(1, operationTaskPage.value + delta))
  if (nextPage === operationTaskPage.value) return
  operationTaskPage.value = nextPage
  loadOperationTasks()
}

function changeOperationLogPage(delta) {
  const nextPage = Math.min(pageCount(operationLogTotal.value, operationLogPageSize.value), Math.max(1, operationLogPage.value + delta))
  if (nextPage === operationLogPage.value) return
  operationLogPage.value = nextPage
  loadOperationLogs()
}

function searchOperationLogs() {
  operationLogPage.value = 1
  loadOperationLogs()
}

function filterOperationTasks() {
  operationTaskPage.value = 1
  loadOperationTasks()
}

function changeOperationTaskPageSize() {
  operationTaskPage.value = 1
  loadOperationTasks()
}

function changeOperationLogPageSize() {
  operationLogPage.value = 1
  loadOperationLogs()
}

function openTodayRedemptions() {
  activeView.value = 'redemptions'
  redemptionFilters.value.today = true
  redemptionPage.value = 1
  selectedRedemptionIds.value = []
}

function clearTodayRedemptionFilter() {
  redemptionFilters.value.today = false
  redemptionPage.value = 1
  loadRedemptions({ clearSelection: true })
}

function openAccountsForCdk(cdk) {
  activeView.value = 'accounts'
  accountFilters.value = {
    q: '',
    status: '',
    has_refresh_token: '',
    email_type: '',
    cdk_id: cdk.id,
    redemption_id: '',
    relation_label: `CDK：${cdkLabel(cdk)}`,
  }
  accountPage.value = 1
  selectedAccountIds.value = []
}

function openAccountsForRedemption(redemption) {
  activeView.value = 'accounts'
  accountFilters.value = {
    q: '',
    status: '',
    has_refresh_token: '',
    email_type: '',
    cdk_id: '',
    redemption_id: redemption.id,
    relation_label: `兑换任务：${redemption.id.slice(0, 8)}`,
  }
  accountPage.value = 1
  selectedAccountIds.value = []
}

function clearAccountRelationFilter() {
  accountFilters.value.cdk_id = ''
  accountFilters.value.redemption_id = ''
  accountFilters.value.relation_label = ''
  accountPage.value = 1
  loadAccounts({ clearSelection: true })
}

async function saveSettings() {
  if (settingsSaving.value) return
  settingsSaving.value = true
  clearSettingsMessageTimer()
  settingsMessage.value = ''
  settingsError.value = ''
  const values = Object.fromEntries(
    systemSettings.value.flatMap((group) => group.items.map((item) => [item.key, item.value])),
  )
  try {
    const result = await adminRequest('/admin/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values }),
    })
    systemSettings.value = result.groups || []
    settingsMessage.value = '系统参数已保存并立即生效。'
    scheduleSettingsMessageClear()
    pushToast('success', '参数已保存', '新的运行配置已经生效。')
    await loadDashboard()
  } catch (error) {
    settingsError.value = error.message
    pushToast('error', '保存参数失败', error.message)
  } finally {
    settingsSaving.value = false
  }
}

function openCdkForAccount(account) {
  const cdk = account.related_cdk
  if (!cdk) return
  activeView.value = 'cdks'
  cdkFilters.value = { q: cdk.code || cdk.prefix, quota: '', status: '' }
  cdkPage.value = 1
  selectedCdkIds.value = []
}

watch(activeView, syncOperationViewPolling)
watch([activeView, adminToken], syncProxyHealthPolling)

function updateSelection(selected, id, checked) {
  if (checked && !selected.value.includes(id)) selected.value = [...selected.value, id]
  if (!checked) selected.value = selected.value.filter((item) => item !== id)
}

function toggleAccount(id, checked) { updateSelection(selectedAccountIds, id, checked) }
function toggleCdk(id, checked) { updateSelection(selectedCdkIds, id, checked) }
function toggleRedemption(id, checked) { updateSelection(selectedRedemptionIds, id, checked) }
function toggleCurrentPage(items, selected, checked) {
  const pageIds = new Set(items.value.map((item) => item.id))
  if (checked) {
    selected.value = [...new Set([...selected.value, ...pageIds])]
    return
  }
  selected.value = selected.value.filter((id) => !pageIds.has(id))
}

function toggleAllAccounts(checked) { toggleCurrentPage(accounts, selectedAccountIds, checked) }
function toggleAllCdks(checked) { toggleCurrentPage(cdks, selectedCdkIds, checked) }
function toggleAllRedemptions(checked) { toggleCurrentPage(redemptions, selectedRedemptionIds, checked) }

function selectionConfig(kind) {
  return {
    accounts: {
      endpoint: '/admin/accounts',
      filters: accountFilters,
      selected: selectedAccountIds,
      total: accountTotal,
      label: '账号',
      allSelected: allAccountResultsSelected,
    },
    cdks: {
      endpoint: '/admin/cdks',
      filters: cdkFilters,
      selected: selectedCdkIds,
      total: cdkTotal,
      label: 'CDK',
      allSelected: allCdkResultsSelected,
    },
    redemptions: {
      endpoint: '/admin/redemptions',
      filters: redemptionFilters,
      selected: selectedRedemptionIds,
      total: redemptionTotal,
      label: '兑换记录',
      allSelected: allRedemptionResultsSelected,
    },
  }[kind]
}

async function toggleAllResults(kind) {
  const config = selectionConfig(kind)
  if (!config || selectionBusy.value) return
  if (config.allSelected.value) {
    config.selected.value = []
    return
  }
  if (config.total.value > maxBulkSelection) {
    pushToast('warning', '无法跨页全选', `当前筛选结果有 ${config.total.value} 个${config.label}，单次最多选择 ${maxBulkSelection} 项。`)
    return
  }
  selectionBusy.value = kind
  try {
    const result = await adminRequest(`${config.endpoint}${listQuery(config.filters.value, 1, 0)}`)
    if (result.total !== result.items.length) throw new ApiError('未能加载全部筛选结果，请缩小筛选范围后重试。')
    config.selected.value = result.items.map((item) => item.id)
  } catch (error) {
    pushToast('error', '跨页全选失败', error.message)
  } finally {
    selectionBusy.value = ''
  }
}

async function selectRandomAccounts() {
  if (selectionBusy.value || accountValidationBusy.value || accountExportBusy.value) return
  const count = Number(randomAccountCount.value)
  const maximum = Math.min(accountTotal.value, maxBulkSelection)
  if (!Number.isInteger(count) || count < 1) {
    pushToast('warning', '请输入选择数量', '请输入一个大于 0 的整数。')
    return
  }
  if (count > maximum) {
    pushToast('warning', '选择数量超出范围', `当前筛选结果最多可随机选择 ${maximum} 个账号。`)
    return
  }

  selectionBusy.value = 'accounts-random'
  try {
    const params = filterParams(accountFilters.value)
    params.set('count', String(count))
    const result = await adminRequest(`/admin/accounts/random-selection${queryString(params)}`)
    selectedAccountIds.value = result.ids
    pushToast('success', '已随机选择账号', `已从当前筛选结果中选择 ${result.count} 个账号。`)
  } catch (error) {
    pushToast('error', '随机选择失败', error.message)
  } finally {
    selectionBusy.value = ''
  }
}

function bulkMessage(result, label) {
  const skipped = result.skipped?.length || 0
  if (!skipped) return `已删除 ${result.deleted} 个${label}。`
  return `已删除 ${result.deleted} 个${label}，${skipped} 个因关联或执行中状态被保留。`
}

function openDeleteDialog(kind) {
  const config = {
    accounts: { count: selectedAccountIds.value.length, label: '账号' },
    cdks: { count: selectedCdkIds.value.length, label: 'CDK' },
    redemptions: { count: selectedRedemptionIds.value.length, label: '兑换记录' },
  }[kind]
  if (!config?.count) return
  deleteDialog.value = { open: true, kind, ...config }
}

function closeDeleteDialog() {
  deleteDialog.value.open = false
}

function openAccountImport() {
  importMessage.value = ''
  accountImportOpen.value = true
}

function openCdkGenerator() {
  cdkMessage.value = ''
  generatedCodes.value = []
  cdkGeneratorOpen.value = true
}

async function confirmDelete() {
  const { kind } = deleteDialog.value
  closeDeleteDialog()
  if (kind === 'accounts') await deleteSelectedAccounts()
  if (kind === 'cdks') await deleteSelectedCdks()
  if (kind === 'redemptions') await deleteSelectedRedemptions()
}

async function deleteSelectedAccounts() {
  if (!selectedAccountIds.value.length) return
  accountMessage.value = ''
  try {
    const result = await adminRequest('/admin/accounts/bulk-delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: selectedAccountIds.value }),
    })
    selectedAccountIds.value = []
    accountMessage.value = bulkMessage(result, '账号')
    pushToast('success', '账号池已更新', accountMessage.value)
    await loadAdminData()
  } catch (error) {
    accountMessage.value = error.message
    pushToast('error', '删除失败', error.message)
  }
}

async function copyCdks(ids) {
  if (!ids.length) return
  cdkMessage.value = ''
  try {
    const result = await adminRequest('/admin/cdks/copy', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }),
    })
    if (result.codes.length) await navigator.clipboard.writeText(result.codes.join('\n'))
    cdkMessage.value = result.codes.length
      ? `已复制 ${result.codes.length} 个 CDK${result.unavailable_ids.length ? `，${result.unavailable_ids.length} 个历史记录不可复制。` : '。'}`
      : '所选 CDK 均不可复制。'
    pushToast(result.codes.length ? 'success' : 'warning', 'CDK 复制', cdkMessage.value)
  } catch (error) {
    cdkMessage.value = error.message
    pushToast('error', '复制失败', error.message)
  }
}

async function copySelectedCdks() {
  await copyCdks(selectedCdkIds.value)
}

async function copySingleCdk(id) {
  await copyCdks([id])
}

function openReissueDialog(cdk) {
  reissueDialog.value = { open: true, id: cdk.id, prefix: cdk.prefix }
}

function closeReissueDialog() {
  reissueDialog.value.open = false
}

async function confirmReissue() {
  const { id } = reissueDialog.value
  closeReissueDialog()
  try {
    const result = await adminRequest('/admin/cdks/reissue', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: [id] }),
    })
    if (!result.codes.length) {
      const message = result.skipped[0]?.reason || '该 CDK 无法重新签发。'
      pushToast('warning', '未重新签发', message)
      return
    }
    await loadAdminData()
    try {
      await navigator.clipboard.writeText(result.codes[0])
      pushToast('success', 'CDK 已重新签发', '新 CDK 已复制，旧 CDK 已失效。')
    } catch {
      pushToast('warning', 'CDK 已重新签发', '浏览器未允许自动复制，请点击该行的复制按钮。')
    }
  } catch (error) {
    pushToast('error', '重新签发失败', error.message)
  }
}

async function deleteSelectedCdks() {
  if (!selectedCdkIds.value.length) return
  cdkMessage.value = ''
  try {
    const result = await adminRequest('/admin/cdks/bulk-delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: selectedCdkIds.value }),
    })
    selectedCdkIds.value = []
    cdkMessage.value = bulkMessage(result, 'CDK')
    pushToast('success', 'CDK 已更新', cdkMessage.value)
    await loadAdminData()
  } catch (error) {
    cdkMessage.value = error.message
    pushToast('error', '删除失败', error.message)
  }
}

async function deleteSelectedRedemptions() {
  if (!selectedRedemptionIds.value.length) return
  redemptionMessage.value = ''
  try {
    const result = await adminRequest('/admin/redemptions/bulk-delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: selectedRedemptionIds.value }),
    })
    selectedRedemptionIds.value = []
    redemptionMessage.value = bulkMessage(result, '兑换记录')
    pushToast('success', '兑换记录已更新', redemptionMessage.value)
    await loadAdminData()
  } catch (error) {
    redemptionMessage.value = error.message
    pushToast('error', '删除失败', error.message)
  }
}

function chooseImportFile() {
  if (importInput.value) importInput.value.value = ''
  importInput.value?.click()
}

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function setImportFiles(files) {
  const nextFiles = Array.from(files || []).filter(Boolean)
  if (!nextFiles.length) return
  importFiles.value = nextFiles
  importPreview.value = null
  importMessage.value = ''
}

function removeImportFile(index) {
  importFiles.value = importFiles.value.filter((_, fileIndex) => fileIndex !== index)
  importPreview.value = null
  importMessage.value = ''
  if (!importFiles.value.length && importInput.value) importInput.value.value = ''
}

function onImportFileChange(event) {
  setImportFiles(event.target.files)
}

function onDrop(event) {
  event.preventDefault()
  setImportFiles(event.dataTransfer.files)
}

async function previewImport() {
  if (!importFiles.value.length) return
  importBusy.value = true
  importMessage.value = ''
  try {
    const form = new FormData()
    importFiles.value.forEach((file) => form.append('file', file, file.name))
    importPreview.value = await adminRequest('/admin/account-imports/preview', { method: 'POST', body: form })
  } catch (error) {
    importMessage.value = error.message
    pushToast('error', '预览失败', error.message)
  } finally {
    importBusy.value = false
  }
}

async function commitImport() {
  if (!importFiles.value.length) return
  importBusy.value = true
  importMessage.value = ''
  try {
    const form = new FormData()
    importFiles.value.forEach((file) => form.append('file', file, file.name))
    form.append('duplicate_strategy', importOptions.value.duplicate_strategy)
    form.append('prevalidate', String(importOptions.value.prevalidate))
    const result = await adminRequest('/admin/account-imports', { method: 'POST', body: form })
    importMessage.value = result.status === 'validating' ? '导入已提交，正在预验活。' : '导入已完成。'
    pushToast('success', '账号导入', importMessage.value)
    importFiles.value = []
    importPreview.value = null
    accountImportOpen.value = false
    if (importInput.value) importInput.value.value = ''
    await loadAdminData()
    if (result.validation_task_id) trackAdminTask(result.validation_task_id)
  } catch (error) {
    importMessage.value = error.message
    pushToast('error', '导入失败', error.message)
  } finally {
    importBusy.value = false
  }
}

async function generateCdks() {
  loading.value = true
  cdkMessage.value = ''
  try {
    const result = await adminRequest('/admin/cdks/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cdkForm.value),
    })
    generatedCodes.value = result.codes
    cdkMessage.value = `已生成 ${result.codes.length} 个 CDK。`
    pushToast('success', 'CDK 已生成', cdkMessage.value)
    await loadAdminData()
  } catch (error) {
    cdkMessage.value = error.message
    pushToast('error', '生成失败', error.message)
  } finally {
    loading.value = false
  }
}

async function copyCodes() {
  try {
    await navigator.clipboard.writeText(generatedCodes.value.join('\n'))
    cdkMessage.value = 'CDK 已复制。'
    pushToast('success', 'CDK 复制', cdkMessage.value)
  } catch (error) {
    cdkMessage.value = error.message
    pushToast('error', '复制失败', error.message)
  }
}

function newIdempotencyKey() {
  return window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
}

async function pollRedemption(id, token) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 700))
    const result = await request(`/redemptions/${id}?token=${encodeURIComponent(token)}`)
    redeemState.value = result
    if (result.status === 'completed') {
      return
    }
    if (result.status === 'failed') throw new ApiError(result.error_message || '兑换失败')
  }
  throw new ApiError('任务等待超时，请稍后使用原任务凭证查询。')
}

function closeExportStream() {
  if (exportSocket) {
    exportSocket.close()
    exportSocket = null
  }
}

async function pollExportTask(path, task, token) {
  for (let attempt = 0; attempt < 1800; attempt += 1) {
    const result = await request(`${path}/${task.id}?token=${encodeURIComponent(token)}`)
    exportTask.value = result
    if (result.status === 'completed') return result
    if (result.status === 'failed') throw new ApiError(result.error_message || '导出失败')
    await new Promise((resolve) => window.setTimeout(resolve, 800))
  }
  throw new ApiError('导出任务等待超时，请稍后重试。')
}

async function waitForExportTask(path, task, token) {
  if (task.status === 'completed') return task
  if (task.status === 'failed') throw new ApiError(task.error_message || '导出失败')
  return new Promise((resolve, reject) => {
    let settled = false
    let fallbackStarted = false
    const settle = (callback, value) => {
      if (settled) return
      settled = true
      closeExportStream()
      callback(value)
    }
    const fallback = async () => {
      if (settled || fallbackStarted) return
      fallbackStarted = true
      try {
        const result = await pollExportTask(path, task, token)
        settle(resolve, result)
      } catch (error) {
        settle(reject, error)
      }
    }
    try {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const socketPath = `${protocol}//${window.location.host}/api/v1${path}/${task.id}/ws?token=${encodeURIComponent(token)}`
      exportSocket = new WebSocket(socketPath)
      exportSocket.onmessage = (event) => {
        try {
          const result = JSON.parse(event.data)
          exportTask.value = result
          if (result.status === 'completed') settle(resolve, result)
          if (result.status === 'failed') settle(reject, new ApiError(result.error_message || '导出失败'))
        } catch (error) {
          settle(reject, error)
        }
      }
      exportSocket.onerror = fallback
      exportSocket.onclose = () => {
        if (!settled) fallback()
      }
    } catch {
      fallback()
    }
  })
}

async function startExportTask(path, token) {
  redeemStage.value = '正在创建导出任务'
  const task = await request(`${path}?token=${encodeURIComponent(token)}`, { method: 'POST' })
  exportTask.value = task
  redeemStage.value = task.status === 'completed' ? '正在开始下载' : '正在打包导出文件'
  const completed = await waitForExportTask(path, task, token)
  exportTask.value = completed
  redeemStage.value = '下载已开始'
  triggerDownload(completed.download_url)
  return completed
}

async function redeem() {
  if (!publicCodes.value.length || redeemBusy.value) return
  redeemBusy.value = true
  redeemError.value = ''
  redeemState.value = null
  exportTask.value = null
  closeExportStream()
  redeemStage.value = '正在校验 CDK'
  const idempotencyKey = newIdempotencyKey()
  try {
    const result = await request('/redemptions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
        Prefer: 'wait=3',
      },
      body: JSON.stringify({ codes: publicCodes.value }),
    })
    redeemState.value = result
    if (result.delivery_type === 'redelivery') {
      redeemStage.value = result.message || 'CDK 已兑换，正在准备补发导出'
      await startExportTask(`/redeliveries/${result.id}/export`, result.task_token)
      redeemState.value = { ...result, status: 'redelivery_completed' }
      return
    }
    if (result.status === 'completed') {
      await startExportTask(`/redemptions/${result.id}/export`, result.task_token)
    } else if (result.status === 'failed') {
      throw new ApiError(result.error_message || '兑换失败')
    } else {
      redeemStage.value = '正在验活并补位'
      await pollRedemption(result.id, result.task_token)
    }
  } catch (error) {
    redeemError.value = error.message
  } finally {
    redeemBusy.value = false
    redeemStage.value = ''
  }
}

function goPublic() {
  window.location.assign('/')
}

onMounted(async () => {
  document.addEventListener('visibilitychange', syncProxyHealthPolling)
  if (isAdmin.value && adminToken.value) await loadAdminData()
  syncProxyHealthPolling()
})

onUnmounted(() => {
  document.removeEventListener('visibilitychange', syncProxyHealthPolling)
  if (proxyHealthPollTimer) window.clearInterval(proxyHealthPollTimer)
  proxyHealthPollTimer = null
  stopAdminTaskPolling()
  clearAdminTaskMessageTimer()
  clearSettingsMessageTimer()
  for (const key of transientMessageTimers.keys()) clearTransientMessageTimer(key)
  stopOperationViewPolling()
  stopOperationTaskDetailPolling()
  closeExportStream()
})
</script>

<template>
  <div v-if="!isAdmin" class="public-page">
    <main class="redeem-shell">
      <div class="redeem-brand"><span class="brand-icon"><KeyRound :size="17" /></span><span>CDK兑换提取</span></div>
      <section class="redeem-title" aria-labelledby="redeem-heading">
        <h1 id="redeem-heading">ChatGPT账号提取</h1>
        <p>输入有效提取码，验证通过后获取账号文件。</p>
      </section>

      <FormSection class="redeem-tool" title="输入 CDK 提取码" description="每行一个 CDK，也支持逗号分隔。" variant="outline" size="md" body-class="redeem-body">
        <textarea id="cdk-input" v-model="codeInput" :disabled="redeemBusy" placeholder="CDK-XXXX-XXXX-XXXX-XXXX&#10;CDK-YYYY-YYYY-YYYY-YYYY" spellcheck="false" />
        <div class="redeem-meta"><span>{{ publicCodes.length ? `已识别 ${publicCodes.length} 个 CDK` : '等待输入' }}</span><span v-if="redeemBusy" class="processing"><LoaderCircle :size="15" class="spin" />{{ redeemStage || publicStatusText }}</span></div>
        <CalloutBox v-if="redeemError" tone="error" variant="soft" size="sm">{{ redeemError }}</CalloutBox>
        <div v-if="redeemState && !redeemError" class="redeem-result"><StatusPill :label="publicStatusText" :tone="statusTone(publicStatus)" size="xs" radius="rounded" /><span v-if="redeemState.delivered_count">{{ redeemState.delivered_count }} 个账号</span></div>
        <div v-if="exportTask && !redeemError" class="export-progress" aria-live="polite">
          <div class="export-progress-meta"><span>{{ exportTask.status === 'completed' ? '导出文件已就绪' : '正在打包导出文件' }}</span><strong>{{ exportTask.processed }} / {{ exportTask.total }}</strong></div>
          <div class="progress-track" role="progressbar" :aria-valuenow="exportTask.percent" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${exportTask.percent}%` }" /></div>
        </div>
        <NButton class="redeem-action" type="button" variant="primary" size="md" block :disabled="!publicCodes.length || redeemBusy" @click="redeem"><CloudDownload v-if="!redeemBusy" :size="17" /><LoaderCircle v-else :size="17" class="spin" />{{ redeemBusy ? '处理中' : '下载 JSON 包' }}</NButton>
      </FormSection>
    </main>
  </div>

  <main v-else-if="!adminToken" class="admin-login">
    <FormSection class="login-surface" title="CDK Loader" description="登录运营控制台以继续操作。" variant="outline" size="md">
      <div class="login-heading"><span class="login-icon"><LockKeyhole :size="21" /></span><div><strong>管理员登录</strong><small>受保护的工作区</small></div></div>
      <form @submit.prevent="login">
        <FormField label="管理员密码"><NInput id="admin-password" v-model="loginPassword" type="password" autocomplete="current-password" placeholder="输入密码" block /></FormField>
        <CalloutBox v-if="loginError" tone="error" variant="soft" size="sm">{{ loginError }}</CalloutBox>
        <NButton type="submit" variant="primary" size="md" block :disabled="loading"><LoaderCircle v-if="loading" :size="17" class="spin" /><ChevronRight v-else :size="17" />进入后台</NButton>
      </form>
    </FormSection>
  </main>

  <div v-else class="admin-page">
    <aside class="admin-sidebar">
      <button class="admin-brand" type="button" @click="goPublic">
        <span class="brand-icon"><KeyRound :size="19" /></span>
        <span><b>CDK Loader</b><small>运营控制台</small></span>
      </button>
      <nav aria-label="管理导航">
        <button v-for="item in navItems" :key="item.id" type="button" :class="['nav-item', { active: activeView === item.id }]" :aria-current="activeView === item.id ? 'page' : undefined" :aria-label="item.label" :title="item.label" @click="activeView = item.id">
          <component :is="item.icon" :size="18" />
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <div class="sidebar-foot"><span>管理员模式</span><button class="sidebar-logout" type="button" title="退出管理后台" @click="logout"><LogOut :size="18" /></button></div>
    </aside>

    <main class="admin-workspace">
      <header class="workspace-header">
        <div>
          <p class="eyebrow">CDK LOADER</p>
          <h1>{{ activePageMeta.title }}</h1>
          <p class="page-description">{{ activePageMeta.description }}</p>
        </div>
        <div class="header-actions"><NButton v-if="activeView === 'accounts'" type="button" variant="outline" size="sm" :disabled="accountExportBusy || accountValidationBusy || !selectedAccountIds.length" @click="exportSelectedAccounts"><LoaderCircle v-if="accountExportBusy" :size="15" class="spin" /><Download v-else :size="15" />导出选中</NButton><NButton v-if="activeView === 'accounts'" type="button" variant="primary" size="sm" @click="openAccountImport"><Upload :size="15" />导入账号</NButton><NButton v-if="activeView === 'cdks'" type="button" variant="primary" size="sm" @click="openCdkGenerator"><KeyRound :size="15" />生成 CDK</NButton><NButton v-if="activeView === 'proxies'" type="button" variant="primary" size="sm" @click="resetProxyGroupForm()"><Plus :size="15" />新建代理组</NButton><NButton v-if="activeView === 'settings'" type="button" variant="primary" size="sm" :disabled="settingsSaving" @click="saveSettings"><LoaderCircle v-if="settingsSaving" :size="15" class="spin" /><Save v-else :size="15" />保存参数</NButton><NButton class="refresh-button" icon-only variant="outline" size="sm" type="button" title="刷新数据" @click="loadAdminData"><RefreshCw :size="16" /></NButton></div>
      </header>

      <section v-if="activeView === 'overview'" class="overview-view">
        <div class="overview-grid">
          <StatCard label="可用账号" :value="dashboard?.accounts.available ?? '-'" caption="可预约交付" icon="lucide:users" icon-tone="success" />
          <StatCard label="预约中" :value="dashboard?.accounts.reserved ?? '-'" caption="兑换处理中" icon="lucide:clock-3" icon-tone="warning" />
          <StatCard label="隔离账号" :value="dashboard?.accounts.quarantined ?? '-'" caption="等待后续确认" icon="lucide:shield-alert" icon-tone="error" />
          <StatCard label="剩余额度" :value="dashboard?.cdk_remaining_quota ?? '-'" caption="全部 CDK" icon="lucide:key-round" icon-tone="info" />
        </div>
        <FormSection class="provider-panel" title="邮箱类型库存" description="查看各类型可用邮箱和对应 CDK 剩余额度。" variant="outline">
          <div class="provider-grid">
            <div class="provider-column">
              <div class="provider-heading"><span class="provider-icon provider-icon-ms"><Mail :size="16" /></span><strong>微软邮箱</strong></div>
              <div class="provider-metrics"><div class="provider-metric"><span>可用邮箱</span><strong>{{ dashboard?.accounts?.available_by_email_type?.ms ?? '-' }}</strong></div><div class="provider-metric"><span>剩余额度</span><strong>{{ dashboard?.cdk_remaining_quota_by_email_type?.ms ?? '-' }}</strong></div></div>
            </div>
            <div class="provider-column">
              <div class="provider-heading"><span class="provider-icon provider-icon-icloud"><Mail :size="16" /></span><strong>苹果邮箱</strong></div>
              <div class="provider-metrics"><div class="provider-metric"><span>可用邮箱</span><strong>{{ dashboard?.accounts?.available_by_email_type?.icloud ?? '-' }}</strong></div><div class="provider-metric"><span>剩余额度</span><strong>{{ dashboard?.cdk_remaining_quota_by_email_type?.icloud ?? '-' }}</strong></div></div>
            </div>
            <div class="provider-column">
              <div class="provider-heading"><span class="provider-icon provider-icon-gmail"><Mail :size="16" /></span><strong>谷歌邮箱</strong></div>
              <div class="provider-metrics"><div class="provider-metric"><span>可用邮箱</span><strong>{{ dashboard?.accounts?.available_by_email_type?.gmail ?? '-' }}</strong></div><div class="provider-metric"><span>剩余额度</span><strong>{{ dashboard?.cdk_remaining_quota_by_email_type?.gmail ?? '-' }}</strong></div></div>
            </div>
          </div>
        </FormSection>
        <FormSection class="system-panel" title="运行状态" description="当前服务运行配置和今日活动。" variant="outline">
          <div class="system-row"><span class="system-label"><Gauge :size="16" />验活模式</span><StatusPill :label="dashboard?.validation_mode || '—'" tone="info" size="xs" radius="rounded" /><span class="system-divider" aria-hidden="true" /><button class="today-redemptions" type="button" title="查看今日兑换记录" @click="openTodayRedemptions"><span class="system-label">今日兑换</span><strong>{{ dashboard?.today_redemptions ?? '-' }}</strong><ChevronRight :size="15" /></button></div>
        </FormSection>
      </section>

      <section v-if="activeView === 'proxies'" class="workspace-section proxy-groups-view">
        <CalloutBox tone="info" variant="soft" size="sm" class="section-message">代理节点地址会加密保存；自动检测发送不含账号凭据的轻量请求，超阈值或连接失败的节点会暂停分配。</CalloutBox>
        <FormSection class="proxy-health-settings" title="自动延时检测" description="以响应头耗时测量连接与 TLS 建连延迟；目标站点非 2xx 响应不会单独隔离节点。" variant="outline" size="sm">
          <div class="proxy-health-controls">
            <div class="proxy-health-toggle"><NCheckbox v-model="proxyHealthSettings.enabled" :disabled="proxyHealthSaving">启用自动检测与隔离</NCheckbox></div>
            <FormField label="检测间隔（分钟）"><NInput v-model.number="proxyHealthSettings.interval_minutes" type="number" min="1" max="30" size="sm" block :disabled="proxyHealthSaving" /></FormField>
            <FormField label="隔离延时阈值（ms）"><NInput v-model.number="proxyHealthSettings.threshold_ms" type="number" min="1" max="60000" size="sm" block :disabled="proxyHealthSaving" /></FormField>
            <div class="proxy-health-save"><NButton type="button" variant="primary" size="sm" :disabled="proxyHealthSaving || !proxyHealthSettingsDirty" @click="saveProxyHealthSettings"><LoaderCircle v-if="proxyHealthSaving" :size="15" class="spin" /><Save v-else :size="15" />保存检测设置</NButton></div>
          </div>
          <small class="setting-description">默认每 5 分钟检测；隔离后约 30 秒复测，连续两次低于阈值的 80% 后恢复。手动测活仅作诊断，不修改自动隔离状态。</small>
        </FormSection>
        <FormSection title="代理组" description="为验活出口维护具名代理组和节点。" variant="outline" size="sm">
          <TableShell class="data-table proxy-groups-table" :show-empty="!proxyGroups.length" :empty-colspan="5" empty-title="暂无代理组" empty-description="新建代理组后可在系统参数中选择。" variant="soft" size="sm">
            <template #head><tr><th>代理组</th><th>策略</th><th>状态</th><th>节点</th><th class="action-cell">操作</th></tr></template>
            <tr v-for="group in proxyGroups" :key="group.id">
              <td><b>{{ group.name }}</b><small>{{ group.id }}</small></td>
              <td>{{ group.strategy === 'round_robin' ? '轮询' : group.strategy === 'time_window' ? `定时轮换 · ${group.rotation_interval_minutes} 分钟` : '请求随机' }}</td>
              <td><StatusPill :label="group.enabled ? '启用' : '停用'" :tone="group.enabled ? 'success' : 'neutral'" size="xs" radius="rounded" /></td>
              <td><div class="proxy-node-summary"><div v-for="node in group.nodes" :key="node.id" :class="['proxy-health-node-line', { disabled: !node.enabled }]">
                <div class="proxy-health-node-copy"><span class="proxy-node-chip" :title="node.url">{{ node.name || '未命名节点' }} · {{ node.url }}</span><div class="proxy-health-node-status"><StatusPill :label="proxyNodeHealthLabel(node)" :tone="proxyNodeHealthTone(node)" size="xs" radius="rounded" /><small>{{ proxyNodeHealthSummary(node, group.id) }}</small></div></div>
                <NButton type="button" variant="outline" size="xs" icon-only title="手动测活" :disabled="Boolean(proxyHealthTestingKey)" @click="testProxyNode(group, node)"><LoaderCircle v-if="proxyHealthTestingKey === proxyHealthTestKey(group.id, node.id)" :size="14" class="spin" /><Activity v-else :size="14" /></NButton>
              </div></div></td>
              <td class="action-cell"><div class="proxy-group-action-buttons"><NButton type="button" variant="outline" size="xs" @click="resetProxyGroupForm(group)"><Save :size="14" />编辑</NButton><NButton type="button" variant="danger" size="xs" icon-only title="删除代理组" :disabled="group.can_delete === false" @click="openDeleteProxyGroup(group)"><Trash2 :size="14" /></NButton></div></td>
            </tr>
          </TableShell>
        </FormSection>
      </section>

      <section v-if="activeView === 'settings'" class="workspace-section settings-view">
        <CalloutBox v-if="settingsMessage" tone="success" variant="soft" size="sm" class="section-message">{{ settingsMessage }}</CalloutBox>
        <CalloutBox v-if="settingsError" tone="error" variant="soft" size="sm" class="section-message">{{ settingsError }}</CalloutBox>
        <div v-if="systemSettings.length" class="settings-sections">
          <FormSection v-for="group in systemSettings" :key="group.key" :title="group.label" variant="outline" size="sm">
            <div class="settings-grid">
              <template v-for="item in group.items" :key="item.key">
                <FormField v-if="isSystemSettingVisible(item)" :label="item.label">
                  <SelectMenu v-if="item.type === 'select' || item.type === 'proxy_group'" :model-value="item.value" :options="item.options" width="full" placement="auto" :aria-label="item.label" @update:model-value="updateSystemSettingValue(item, $event)" />
                  <NInput v-else v-model="item.value" :type="item.type === 'integer' || item.type === 'number' ? 'number' : 'text'" :min="item.min ?? undefined" :max="item.max ?? undefined" :step="item.type === 'number' ? 'any' : '1'" size="sm" block :aria-label="item.label" />
                  <small class="setting-description">{{ item.description }}<span v-if="item.unit"> · {{ item.unit }}</span></small>
                </FormField>
              </template>
            </div>
          </FormSection>
        </div>
        <CalloutBox v-else-if="!settingsError" tone="info" variant="soft" size="sm"><LoaderCircle :size="15" class="spin" />正在读取系统参数...</CalloutBox>
      </section>

      <section v-if="activeView === 'accounts'" class="workspace-section">
        <ToolbarShell class="list-toolbar" stack-on-mobile>
          <template #start><div class="selection-actions"><div class="list-summary"><span>当前结果</span><strong>{{ accountTotal }}</strong><span>个账号</span></div><div v-if="accountTotal" class="random-selection-control"><NInput id="random-account-count" name="random-account-count" v-model="randomAccountCount" type="number" min="1" :max="Math.min(accountTotal, maxBulkSelection)" size="sm" class="random-count-input" placeholder="数量" aria-label="随机选择账号数量" @keyup.enter="selectRandomAccounts" /><NButton type="button" variant="outline" size="xs" :disabled="Boolean(selectionBusy) || accountValidationBusy || accountExportBusy" title="从当前筛选结果中随机选择指定数量账号" @click="selectRandomAccounts"><LoaderCircle v-if="selectionBusy === 'accounts-random'" :size="15" class="spin" /><Dices v-else :size="15" />随机选择</NButton></div><NButton v-if="accountTotal > accounts.length" type="button" variant="outline" size="xs" :disabled="Boolean(selectionBusy) || accountValidationBusy || accountExportBusy" @click="toggleAllResults('accounts')"><LoaderCircle v-if="selectionBusy === 'accounts'" :size="15" class="spin" /><ListChecks v-else :size="15" />{{ allAccountResultsSelected ? '取消全部选择' : `选择全部 ${accountTotal} 项` }}</NButton><span v-if="selectedAccountIds.length" class="selection-count">已选择 {{ selectedAccountIds.length }} 项</span><NButton v-if="selectedAccountIds.length" type="button" variant="outline" size="xs" :disabled="accountValidationBusy || accountExportBusy" @click="validateSelectedAccounts"><LoaderCircle v-if="accountValidationBusy" :size="15" class="spin" /><ShieldCheck v-else :size="15" />批量验活</NButton><NButton v-if="selectedAccountIds.length" type="button" variant="danger" size="xs" icon-only title="删除选中账号" :disabled="accountValidationBusy || accountExportBusy" @click="openDeleteDialog('accounts')"><Trash2 :size="15" /></NButton></div></template>
          <template #end><div class="filter-group"><span v-if="accountFilters.relation_label" class="relation-filter"><span :title="accountFilters.relation_label">{{ accountFilters.relation_label }}</span><NButton type="button" variant="outline" size="xs" icon-only title="清除关联筛选" @click="clearAccountRelationFilter"><X :size="14" /></NButton></span><div class="search-field"><Search :size="15" /><NInput id="account-search" name="account-search" v-model="accountFilters.q" size="sm" placeholder="搜索账号、来源或 ID" aria-label="搜索账号" @keyup.enter="searchAccounts" /></div><FilterSelect v-model="accountFilters.email_type" :options="accountEmailTypeOptions" size="sm" aria-label="邮箱类型筛选" @update:model-value="searchAccounts" /><FilterSelect v-model="accountFilters.has_refresh_token" :options="refreshTokenOptions" size="sm" aria-label="Refresh Token 筛选" @update:model-value="searchAccounts" /><FilterSelect v-model="accountFilters.status" :options="accountStatusOptions" size="sm" aria-label="账号状态筛选" @update:model-value="searchAccounts" /></div></template>
        </ToolbarShell>
        <CalloutBox v-if="accountMessage" :tone="messageTone(accountMessage)" variant="soft" size="sm" class="section-message">{{ accountMessage }}</CalloutBox>
        <TableShell class="data-table accounts-table" :show-empty="!accounts.length" :empty-colspan="8" empty-title="暂无账号" empty-description="导入账号后会显示在这里。" variant="soft" size="sm"><template #head><tr><th class="selection-cell"><NCheckbox :model-value="allAccountsSelected" :indeterminate="selectedAccountsOnPage > 0 && !allAccountsSelected" aria-label="选择当前页账号" @update:model-value="toggleAllAccounts" /></th><th>账号</th><th>关联 CDK</th><th>来源</th><th>凭据</th><th>时间（东八区）</th><th>状态</th><th class="action-cell">操作</th></tr></template><tr v-for="account in accounts" :key="account.id"><td class="selection-cell"><NCheckbox :model-value="selectedAccountIds.includes(account.id)" :aria-label="`选择 ${account.email}`" @update:model-value="toggleAccount(account.id, $event)" /></td><td><b>{{ account.email || '—' }}</b><small>{{ account.account_id || '—' }}</small></td><td class="relation-cell"><button v-if="account.related_cdk" class="relation-code" type="button" :title="`查看 ${cdkLabel(account.related_cdk)}`" @click="openCdkForAccount(account)">{{ cdkLabel(account.related_cdk) }}</button><small v-if="account.related_cdk">任务 {{ account.related_cdk.redemption_id.slice(0, 8) }}</small><span v-else>—</span></td><td>{{ account.source }}</td><td>{{ account.has_access_token ? 'AT' : '' }}{{ account.has_refresh_token ? ' · RT' : '' }}</td><td><b>{{ formatDate(account.delivered_at || account.validated_at) }}</b><small>{{ account.delivered_at ? '交付时间' : '验活时间' }}</small></td><td><StatusPill :label="statusLabel(account.status)" :tone="statusTone(account.status)" size="xs" radius="rounded" /></td><td class="action-cell"><NButton v-if="account.related_cdk" type="button" variant="outline" size="xs" icon-only title="二次导出账号" :disabled="accountExportBusy" @click="reexportAccount(account)"><LoaderCircle v-if="accountExportBusy" :size="15" class="spin" /><Download v-else :size="15" /></NButton><span v-else>—</span></td></tr></TableShell>
        <div v-if="accountTotal" class="pagination-bar" aria-label="账号池分页"><span>{{ pageRange(accountTotal, accountPage, accountPageSize) }}</span><div class="pagination-controls"><FilterSelect class="page-size-select" v-model="accountPageSize" :options="pageSizeOptions" size="sm" aria-label="账号池每页条数" @update:model-value="changeAccountPageSize" /><NButton type="button" variant="outline" size="xs" icon-only title="上一页" :disabled="accountPage <= 1" @click="changeAccountPage(-1)"><ChevronLeft :size="15" /></NButton><strong>{{ accountPage }} / {{ pageCount(accountTotal, accountPageSize) }}</strong><NButton type="button" variant="outline" size="xs" icon-only title="下一页" :disabled="accountPage >= pageCount(accountTotal, accountPageSize)" @click="changeAccountPage(1)"><ChevronRight :size="15" /></NButton></div></div>
      </section>

      <section v-if="activeView === 'cdks'" class="workspace-section">
        <ToolbarShell class="list-toolbar" stack-on-mobile>
          <template #start><div class="selection-actions"><div class="list-summary"><span>当前结果</span><strong>{{ cdkTotal }}</strong><span>个 CDK</span></div><NButton v-if="cdkTotal > cdks.length" type="button" variant="outline" size="xs" :disabled="Boolean(selectionBusy)" @click="toggleAllResults('cdks')"><LoaderCircle v-if="selectionBusy === 'cdks'" :size="15" class="spin" /><ListChecks v-else :size="15" />{{ allCdkResultsSelected ? '取消全部选择' : `选择全部 ${cdkTotal} 项` }}</NButton><span v-if="selectedCdkIds.length" class="selection-count">已选择 {{ selectedCdkIds.length }} 项</span><NButton v-if="selectedCdkIds.length" type="button" variant="outline" size="xs" icon-only title="复制选中 CDK" @click="copySelectedCdks"><Clipboard :size="15" /></NButton><NButton v-if="selectedCdkIds.length" type="button" variant="danger" size="xs" icon-only title="删除选中 CDK" @click="openDeleteDialog('cdks')"><Trash2 :size="15" /></NButton></div></template>
          <template #end><div class="filter-group"><div class="search-field"><Search :size="15" /><NInput v-model="cdkFilters.q" size="sm" placeholder="搜索完整 CDK 或前缀" aria-label="搜索 CDK" @keyup.enter="searchCdks" /></div><div class="quota-search-field"><Gauge :size="15" /><NInput v-model="cdkFilters.quota" size="sm" placeholder="额度：10 或 0/10" aria-label="按额度筛选 CDK" @keyup.enter="searchCdks" /></div><FilterSelect v-model="cdkFilters.email_type" :options="cdkFilterEmailTypeOptions" size="sm" aria-label="邮箱类型筛选" @update:model-value="searchCdks" /><FilterSelect v-model="cdkFilters.status" :options="cdkStatusOptions" size="sm" aria-label="CDK 状态筛选" @update:model-value="searchCdks" /></div></template>
        </ToolbarShell>
        <CalloutBox v-if="cdkFilterMessage" tone="warning" variant="soft" size="sm" class="section-message">{{ cdkFilterMessage }}</CalloutBox>
        <TableShell class="data-table cdks-table" :show-empty="!cdks.length" :empty-colspan="10" empty-title="暂无 CDK" empty-description="生成 CDK 后会显示在这里。" variant="soft" size="sm"><template #head><tr><th class="selection-cell"><NCheckbox :model-value="allCdksSelected" :indeterminate="selectedCdksOnPage > 0 && !allCdksSelected" aria-label="选择当前页 CDK" @update:model-value="toggleAllCdks" /></th><th>CDK</th><th>邮箱类型</th><th>剩余 / 总额度</th><th>冻结</th><th>格式</th><th>有效期</th><th>已兑换账号</th><th>状态</th><th class="action-cell">操作</th></tr></template><tr v-for="cdk in cdks" :key="cdk.id"><td class="selection-cell"><NCheckbox :model-value="selectedCdkIds.includes(cdk.id)" :aria-label="`选择 ${cdk.prefix}`" @update:model-value="toggleCdk(cdk.id, $event)" /></td><td><b>{{ cdkLabel(cdk) }}</b><small v-if="!cdk.code">历史明文不可恢复</small></td><td><StatusPill :label="emailTypeLabel(cdk.email_type)" tone="info" size="xs" radius="rounded" /></td><td>{{ cdk.remaining_quota }} / {{ cdk.total_quota }}</td><td>{{ cdk.reserved_quota }}</td><td>{{ cdk.export_format.toUpperCase() }}</td><td>{{ cdk.expires_at ? formatDate(cdk.expires_at, 'date') : '长期' }}</td><td class="relation-cell"><button v-if="cdk.delivery_count" class="relation-count" type="button" :title="`查看 ${cdkLabel(cdk)} 关联的账号`" @click="openAccountsForCdk(cdk)"><Users :size="14" /><span>{{ cdk.delivery_count }} 个账号</span></button><span v-else>—</span></td><td><StatusPill :label="statusLabel(cdk.status)" :tone="statusTone(cdk.status)" size="xs" radius="rounded" /></td><td class="action-cell"><NButton v-if="cdk.can_copy" type="button" variant="outline" size="xs" icon-only title="复制完整 CDK" @click="copySingleCdk(cdk.id)"><Clipboard :size="15" /></NButton><NButton v-else-if="cdk.status === 'unused' && cdk.remaining_quota === cdk.total_quota && cdk.reserved_quota === 0" type="button" variant="outline" size="xs" icon-only title="重新签发并复制" @click="openReissueDialog(cdk)"><RefreshCw :size="15" /></NButton><span v-else>—</span></td></tr></TableShell>
        <div v-if="cdkTotal" class="pagination-bar" aria-label="CDK 分页"><span>{{ pageRange(cdkTotal, cdkPage, cdkPageSize) }}</span><div class="pagination-controls"><FilterSelect class="page-size-select" v-model="cdkPageSize" :options="pageSizeOptions" size="sm" aria-label="CDK 每页条数" @update:model-value="changeCdkPageSize" /><NButton type="button" variant="outline" size="xs" icon-only title="上一页" :disabled="cdkPage <= 1" @click="changeCdkPage(-1)"><ChevronLeft :size="15" /></NButton><strong>{{ cdkPage }} / {{ pageCount(cdkTotal, cdkPageSize) }}</strong><NButton type="button" variant="outline" size="xs" icon-only title="下一页" :disabled="cdkPage >= pageCount(cdkTotal, cdkPageSize)" @click="changeCdkPage(1)"><ChevronRight :size="15" /></NButton></div></div>
      </section>

      <section v-if="activeView === 'redemptions'" class="workspace-section">
        <ToolbarShell class="list-toolbar" stack-on-mobile>
          <template #start><div class="selection-actions"><div class="list-summary"><span>当前结果</span><strong>{{ redemptionTotal }}</strong><span>条记录</span></div><NButton v-if="redemptionTotal > redemptions.length" type="button" variant="outline" size="xs" :disabled="Boolean(selectionBusy)" @click="toggleAllResults('redemptions')"><LoaderCircle v-if="selectionBusy === 'redemptions'" :size="15" class="spin" /><ListChecks v-else :size="15" />{{ allRedemptionResultsSelected ? '取消全部选择' : `选择全部 ${redemptionTotal} 项` }}</NButton><span v-if="selectedRedemptionIds.length" class="selection-count">已选择 {{ selectedRedemptionIds.length }} 项</span><NButton v-if="selectedRedemptionIds.length" type="button" variant="danger" size="xs" icon-only title="删除选中兑换记录" @click="openDeleteDialog('redemptions')"><Trash2 :size="15" /></NButton></div></template>
          <template #end><div class="filter-group"><span v-if="redemptionFilters.today" class="today-filter"><StatusPill label="仅今天" tone="info" size="xs" radius="rounded" /><NButton type="button" variant="outline" size="xs" icon-only title="清除今日筛选" @click="clearTodayRedemptionFilter"><X :size="14" /></NButton></span><div class="search-field"><Search :size="15" /><NInput v-model="redemptionFilters.q" size="sm" placeholder="搜索任务 ID 或完整 CDK" aria-label="搜索兑换记录" @keyup.enter="searchRedemptions" /></div><FilterSelect v-model="redemptionFilters.status" :options="redemptionStatusOptions" size="sm" aria-label="兑换状态筛选" @update:model-value="searchRedemptions" /></div></template>
        </ToolbarShell>
        <CalloutBox v-if="redemptionMessage" :tone="messageTone(redemptionMessage)" variant="soft" size="sm" class="section-message">{{ redemptionMessage }}</CalloutBox>
        <TableShell class="data-table redemptions-table" :show-empty="!redemptions.length" :empty-colspan="6" empty-title="暂无兑换记录" empty-description="用户兑换后会显示在这里。" variant="soft" size="sm"><template #head><tr><th class="selection-cell"><NCheckbox :model-value="allRedemptionsSelected" :indeterminate="selectedRedemptionsOnPage > 0 && !allRedemptionsSelected" aria-label="选择当前页兑换记录" @update:model-value="toggleAllRedemptions" /></th><th>任务</th><th>CDK</th><th>请求 / 交付</th><th>创建时间（东八区）</th><th>状态</th></tr></template><tr v-for="item in redemptions" :key="item.id"><td class="selection-cell"><NCheckbox :model-value="selectedRedemptionIds.includes(item.id)" :aria-label="`选择兑换任务 ${item.id.slice(0, 8)}`" @update:model-value="toggleRedemption(item.id, $event)" /></td><td><b>{{ item.id.slice(0, 8) }}</b><small>{{ item.error_message || '—' }}</small></td><td class="relation-cell"><div class="relation-stack"><button v-for="cdk in item.cdks" :key="cdk.id" class="relation-code" type="button" :title="`查看 ${cdkLabel(cdk)} 关联的账号`" @click="openAccountsForCdk(cdk)">{{ cdkLabel(cdk) }}</button><span v-if="!item.cdks.length">—</span></div></td><td><div class="delivery-summary"><span>{{ item.requested_count }} / {{ item.delivered_count }}</span><button v-if="item.delivered_count" class="relation-count" type="button" title="查看本次兑换交付的账号" @click="openAccountsForRedemption(item)"><Users :size="14" /><span>查看账号</span></button></div></td><td>{{ formatDate(item.created_at) }}</td><td><StatusPill :label="statusLabel(item.status)" :tone="statusTone(item.status)" size="xs" radius="rounded" /></td></tr></TableShell>
        <div v-if="redemptionTotal" class="pagination-bar" aria-label="兑换记录分页"><span>{{ pageRange(redemptionTotal, redemptionPage, redemptionPageSize) }}</span><div class="pagination-controls"><FilterSelect class="page-size-select" v-model="redemptionPageSize" :options="pageSizeOptions" size="sm" aria-label="兑换记录每页条数" @update:model-value="changeRedemptionPageSize" /><NButton type="button" variant="outline" size="xs" icon-only title="上一页" :disabled="redemptionPage <= 1" @click="changeRedemptionPage(-1)"><ChevronLeft :size="15" /></NButton><strong>{{ redemptionPage }} / {{ pageCount(redemptionTotal, redemptionPageSize) }}</strong><NButton type="button" variant="outline" size="xs" icon-only title="下一页" :disabled="redemptionPage >= pageCount(redemptionTotal, redemptionPageSize)" @click="changeRedemptionPage(1)"><ChevronRight :size="15" /></NButton></div></div>
      </section>

      <section v-if="activeView === 'logs'" class="workspace-section logs-view">
        <div class="logs-task-subsection">
          <ToolbarShell class="list-toolbar" stack-on-mobile>
            <template #start><div class="selection-actions"><div class="list-summary"><span>任务</span><strong>{{ operationTaskTotal }}</strong><span>条</span></div><RefreshCw :size="15" class="task-refresh-icon" /><span v-if="accountValidationTask?.status === 'running'" class="task-live-label">验活任务实时更新</span></div></template>
            <template #end><div class="filter-group"><FilterSelect v-model="operationTaskFilters.task_type" :options="[{ label: '全部任务', value: '' }, { label: '手动验活', value: 'manual_validation' }, { label: '导入预验活', value: 'account_import_validation' }, { label: '兑换导出', value: 'redemption_export' }, { label: '补发导出', value: 'redelivery_export' } ]" size="sm" aria-label="任务类型筛选" @update:model-value="filterOperationTasks" /><FilterSelect v-model="operationTaskFilters.status" :options="[{ label: '全部状态', value: '' }, { label: '排队中', value: 'queued' }, { label: '处理中', value: 'running' }, { label: '已完成', value: 'completed' }, { label: '失败', value: 'failed' } ]" size="sm" aria-label="任务状态筛选" @update:model-value="filterOperationTasks" /></div></template>
          </ToolbarShell>
          <TableShell class="data-table operation-tasks-table" :show-empty="!operationTasks.length" :empty-colspan="6" empty-title="暂无操作任务" empty-description="验活或导出后会显示任务进度。" variant="soft" size="sm">
            <template #head><tr><th>任务</th><th>类型</th><th>进度</th><th>结果</th><th>创建时间（东八区）</th><th>状态</th></tr></template>
            <tr v-for="task in operationTasks" :key="task.id" class="task-row">
              <td><button class="task-detail-link" type="button" title="查看任务详情" @click="openOperationTask(task)"><b>{{ task.id.slice(0, 8) }}</b><small>{{ task.resource_id ? `资源 ${task.resource_id.slice(0, 8)}` : '—' }}</small></button></td>
              <td>{{ taskTypeLabel(task.task_type) }}</td>
              <td><div class="task-progress"><div class="progress-track"><span :style="{ width: `${task.percent}%` }" /></div><small>{{ task.processed }} / {{ task.total }}（{{ task.percent }}%）</small></div></td>
              <td><span class="task-result-counts">有效 {{ task.valid_count }} · 无效 {{ task.invalid_count }} · 待确认 {{ task.inconclusive_count }} · RT 恢复 {{ task.recoverable_count }} · 跳过 {{ task.skipped_count }}</span></td>
              <td>{{ formatDate(task.created_at) }}</td>
              <td><StatusPill :label="statusLabel(task.status)" :tone="statusTone(task.status)" size="xs" radius="rounded" /></td>
            </tr>
          </TableShell>
          <div v-if="operationTaskTotal" class="pagination-bar" aria-label="操作任务分页"><span>{{ pageRange(operationTaskTotal, operationTaskPage, operationTaskPageSize) }}</span><div class="pagination-controls"><FilterSelect class="page-size-select" v-model="operationTaskPageSize" :options="pageSizeOptions" size="sm" aria-label="操作任务每页条数" @update:model-value="changeOperationTaskPageSize" /><NButton type="button" variant="outline" size="xs" icon-only title="上一页" :disabled="operationTaskPage <= 1" @click="changeOperationTaskPage(-1)"><ChevronLeft :size="15" /></NButton><strong>{{ operationTaskPage }} / {{ pageCount(operationTaskTotal, operationTaskPageSize) }}</strong><NButton type="button" variant="outline" size="xs" icon-only title="下一页" :disabled="operationTaskPage >= pageCount(operationTaskTotal, operationTaskPageSize)" @click="changeOperationTaskPage(1)"><ChevronRight :size="15" /></NButton></div></div>
        </div>
        <div class="logs-subsection">
          <ToolbarShell class="list-toolbar" stack-on-mobile>
            <template #start><div class="selection-actions"><div class="list-summary"><span>日志</span><strong>{{ operationLogTotal }}</strong><span>条</span></div></div></template>
            <template #end><div class="filter-group"><div class="search-field"><Search :size="15" /><NInput v-model="operationLogFilters.q" size="sm" placeholder="搜索任务、账号或消息" aria-label="搜索操作日志" @keyup.enter="searchOperationLogs" /></div><FilterSelect v-model="operationLogFilters.operation_type" :options="[{ label: '全部操作', value: '' }, { label: '手动验活', value: 'manual_validation' }, { label: '导入预验活', value: 'account_import_validation' }, { label: '兑换导出', value: 'redemption_export' }, { label: '补发导出', value: 'redelivery_export' }, { label: '账号导入', value: 'account_import' }, { label: '兑换', value: 'redemption' } ]" size="sm" aria-label="操作类型筛选" @update:model-value="searchOperationLogs" /><FilterSelect v-model="operationLogFilters.outcome" :options="[{ label: '全部结果', value: '' }, { label: '有效', value: 'valid' }, { label: '无效', value: 'invalid' }, { label: '待确认', value: 'inconclusive' }, { label: '跳过', value: 'skipped' }, { label: '失败', value: 'failed' }, { label: '已完成', value: 'completed' } ]" size="sm" aria-label="日志结果筛选" @update:model-value="searchOperationLogs" /></div></template>
          </ToolbarShell>
          <TableShell class="data-table operation-logs-table" :show-empty="!operationLogs.length" :empty-colspan="5" empty-title="暂无操作日志" empty-description="当前筛选没有匹配记录。" variant="soft" size="sm"><template #head><tr><th>时间（东八区）</th><th>操作</th><th>账号</th><th>结果</th><th>消息</th></tr></template><tr v-for="log in operationLogs" :key="log.id"><td>{{ formatDate(log.created_at) }}</td><td><b>{{ taskTypeLabel(log.operation_type) }}</b><small>{{ log.task_id ? `任务 ${log.task_id.slice(0, 8)}` : (log.resource_id ? `资源 ${log.resource_id.slice(0, 8)}` : '—') }}</small></td><td><b>{{ log.account_email || '—' }}</b><small>{{ log.account_id || '—' }}</small></td><td><StatusPill :label="outcomeLabel(log.outcome)" :tone="statusTone(log.outcome)" size="xs" radius="rounded" /></td><td><span class="log-message" :title="log.message">{{ log.message || '—' }}</span></td></tr></TableShell>
          <div v-if="operationLogTotal" class="pagination-bar" aria-label="操作日志分页"><span>{{ pageRange(operationLogTotal, operationLogPage, operationLogPageSize) }}</span><div class="pagination-controls"><FilterSelect class="page-size-select" v-model="operationLogPageSize" :options="pageSizeOptions" size="sm" aria-label="操作日志每页条数" @update:model-value="changeOperationLogPageSize" /><NButton type="button" variant="outline" size="xs" icon-only title="上一页" :disabled="operationLogPage <= 1" @click="changeOperationLogPage(-1)"><ChevronLeft :size="15" /></NButton><strong>{{ operationLogPage }} / {{ pageCount(operationLogTotal, operationLogPageSize) }}</strong><NButton type="button" variant="outline" size="xs" icon-only title="下一页" :disabled="operationLogPage >= pageCount(operationLogTotal, operationLogPageSize)" @click="changeOperationLogPage(1)"><ChevronRight :size="15" /></NButton></div></div>
        </div>
      </section>
    </main>
  </div>

  <ModalShell :open="proxyGroupOpen" :title="proxyGroupForm.id ? '编辑代理组' : '新建代理组'" description="配置节点地址、启用状态和节点选择策略。" max-width="820px" @close="proxyGroupOpen = false">
    <div class="proxy-group-dialog-body">
      <div class="proxy-group-form-grid">
        <FormField label="代理组名称"><NInput v-model="proxyGroupForm.name" size="sm" block placeholder="例如：海外验活出口" /></FormField>
        <FormField label="节点策略"><SelectMenu v-model="proxyGroupForm.strategy" :options="[{ label: '请求随机', value: 'request_random' }, { label: '轮询', value: 'round_robin' }, { label: '定时轮换', value: 'time_window' }]" width="full" placement="auto" aria-label="节点策略" /></FormField>
        <FormField v-if="proxyGroupForm.strategy === 'time_window'" label="轮换间隔"><NInput v-model.number="proxyGroupForm.rotation_interval_minutes" type="number" min="1" max="1440" size="sm" block /><small class="setting-description">单位：分钟</small></FormField>
        <div class="proxy-group-enabled"><NCheckbox v-model="proxyGroupForm.enabled">启用代理组</NCheckbox></div>
      </div>
      <div class="proxy-node-editor">
        <div class="proxy-node-editor-heading"><div><strong>代理节点</strong><small>认证信息不会在列表中回显；编辑时保留未修改的脱敏地址即可。</small></div><div class="proxy-node-editor-actions"><NButton type="button" variant="outline" size="xs" @click="openProxyGroupBatch"><Upload :size="14" />批量添加</NButton><NButton type="button" variant="outline" size="xs" @click="addProxyGroupNode"><Plus :size="14" />添加节点</NButton></div></div>
        <div v-if="proxyGroupBatchOpen" class="proxy-batch-editor">
          <div class="proxy-batch-heading"><strong>批量添加代理节点</strong><small>每行一个代理地址；地址后的文字会作为节点名称。空行和以 # 开头的行会跳过。</small></div>
          <textarea v-model="proxyGroupBatchText" class="proxy-batch-textarea" rows="7" placeholder="http://127.0.0.1:7890&#10;http://user:password@host:7890 海外节点&#10;socks5://127.0.0.1:1080" aria-label="批量代理地址" spellcheck="false" autocomplete="off" />
          <CalloutBox v-if="proxyGroupBatchMessage" tone="success" variant="soft" size="sm">{{ proxyGroupBatchMessage }}</CalloutBox>
          <CalloutBox v-if="proxyGroupBatchError" tone="error" variant="soft" size="sm">{{ proxyGroupBatchError }}</CalloutBox>
          <div class="proxy-batch-actions"><NButton type="button" variant="outline" size="xs" @click="closeProxyGroupBatch">取消</NButton><NButton type="button" variant="primary" size="xs" @click="appendProxyGroupBatch"><Plus :size="14" />添加到节点列表</NButton></div>
        </div>
        <div v-for="(node, index) in proxyGroupForm.nodes" :key="node.id || `new-${index}`" class="proxy-node-form-row">
          <NInput v-model="node.name" size="sm" placeholder="节点名称" aria-label="节点名称" />
          <NInput v-model="node.url" size="sm" block placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:1080" aria-label="代理地址" />
          <NCheckbox v-model="node.enabled" aria-label="启用节点">启用</NCheckbox>
          <NButton type="button" variant="danger" size="xs" icon-only title="移除节点" :disabled="proxyGroupForm.nodes.length <= 1" @click="removeProxyGroupNode(index)"><Trash2 :size="14" /></NButton>
        </div>
      </div>
      <CalloutBox v-if="proxyGroupError" tone="error" variant="soft" size="sm">{{ proxyGroupError }}</CalloutBox>
    </div>
    <template #footer><NButton type="button" variant="outline" size="sm" @click="proxyGroupOpen = false">取消</NButton><NButton type="button" variant="primary" size="sm" :disabled="proxyGroupSaving" @click="saveProxyGroup"><LoaderCircle v-if="proxyGroupSaving" :size="15" class="spin" /><Save v-else :size="15" />保存代理组</NButton></template>
  </ModalShell>

  <ModalShell :open="operationTaskDetailOpen" title="任务详情" :description="operationTaskDetail ? `${taskTypeLabel(operationTaskDetail.task_type)} · 任务 ${operationTaskDetail.id.slice(0, 8)}` : '读取任务详情'" max-width="980px" @close="closeOperationTaskDetail">
    <div v-if="operationTaskDetail" class="operation-task-detail">
      <div class="task-detail-overview">
        <div class="task-detail-status"><span>状态</span><StatusPill :label="statusLabel(operationTaskDetail.status)" :tone="statusTone(operationTaskDetail.status)" size="xs" radius="rounded" /></div>
        <div><span>处理进度</span><strong>{{ operationTaskDetail.processed }} / {{ operationTaskDetail.total }}（{{ operationTaskDetail.percent }}%）</strong></div>
        <div><span>创建时间</span><strong>{{ formatDate(operationTaskDetail.created_at) }}</strong></div>
        <div><span>完成时间</span><strong>{{ formatDate(operationTaskDetail.completed_at) }}</strong></div>
      </div>
      <div class="task-detail-progress">
        <div class="progress-track"><span :style="{ width: `${operationTaskDetail.percent}%` }" /></div>
        <div class="task-detail-counts"><span>有效 <b>{{ operationTaskDetail.valid_count }}</b></span><span>无效 <b>{{ operationTaskDetail.invalid_count }}</b></span><span>待确认 <b>{{ operationTaskDetail.inconclusive_count }}</b></span><span>RT 恢复 <b>{{ operationTaskDetail.recoverable_count }}</b></span><span>跳过 <b>{{ operationTaskDetail.skipped_count }}</b></span><span>失败 <b>{{ operationTaskDetail.failed_count }}</b></span></div>
      </div>
      <CalloutBox v-if="operationTaskDetail.error_message" tone="error" variant="soft" size="sm">{{ operationTaskDetail.error_message }}</CalloutBox>
      <CalloutBox v-if="operationTaskDetailError" tone="error" variant="soft" size="sm">{{ operationTaskDetailError }}</CalloutBox>
      <div class="task-detail-log-section">
        <div class="task-detail-section-heading"><div><b>执行记录</b><span>按时间倒序显示最近 100 条</span></div><LoaderCircle v-if="operationTaskDetailLoading" :size="15" class="spin" /></div>
        <TableShell class="data-table operation-detail-logs" :show-empty="!operationTaskDetail.logs?.length" :empty-colspan="4" empty-title="暂无执行记录" empty-description="任务开始后会显示逐条结果。" variant="soft" size="sm"><template #head><tr><th>时间（东八区）</th><th>账号</th><th>结果</th><th>消息</th></tr></template><tr v-for="log in operationTaskDetail.logs" :key="log.id"><td>{{ formatDate(log.created_at) }}</td><td><b>{{ log.account_email || '—' }}</b><small>{{ log.account_id || '—' }}</small></td><td><StatusPill :label="outcomeLabel(log.outcome)" :tone="statusTone(log.outcome)" size="xs" radius="rounded" /></td><td><span class="task-detail-message">{{ log.message || '—' }}</span></td></tr></TableShell>
      </div>
    </div>
    <CalloutBox v-else-if="operationTaskDetailLoading" tone="info" variant="soft" size="sm"><LoaderCircle :size="15" class="spin" />正在读取任务详情...</CalloutBox>
    <CalloutBox v-else tone="error" variant="soft" size="sm">{{ operationTaskDetailError || '任务详情暂时不可用。' }}</CalloutBox>
    <template #footer><NButton type="button" variant="outline" size="sm" @click="closeOperationTaskDetail">关闭</NButton></template>
  </ModalShell>

  <ModalShell :open="accountImportOpen" title="导入账号" description="上传账号文件，预览无误后写入账号池。" max-width="880px" @close="accountImportOpen = false">
    <div class="import-dialog-body">
      <div class="import-file-picker">
        <div class="dropzone" :class="{ occupied: importFiles.length }" @dragover.prevent @drop="onDrop" @click="chooseImportFile">
          <input ref="importInput" type="file" accept=".json,.csv,.txt,.zip" multiple hidden @change="onImportFileChange" />
          <FileUp :size="24" />
          <strong>{{ importFiles.length ? `已选择 ${importFiles.length} 个文件` : '选择账号文件' }}</strong>
          <span>{{ importFiles.length ? `总计 ${formatFileSize(importTotalSize)}` : 'JSON · CSV · TXT · ZIP' }}</span>
        </div>
        <div v-if="importFiles.length" class="import-file-list" aria-label="已选择的账号文件">
          <div v-for="(file, index) in importFiles" :key="`${file.name}-${file.lastModified}-${file.size}-${index}`" class="import-file-row">
            <span class="import-file-name" :title="file.name">{{ file.name }}</span>
            <span class="import-file-size">{{ formatFileSize(file.size) }}</span>
            <button class="import-file-remove" type="button" :aria-label="`移除 ${file.name}`" title="移除文件" @click.stop="removeImportFile(index)"><X :size="14" /></button>
          </div>
        </div>
      </div>
      <div class="import-dialog-options">
        <FormField label="重复账号"><FilterSelect v-model="importOptions.duplicate_strategy" :options="[{ label: '跳过', value: 'skip' }, { label: '补充空字段', value: 'fill_missing' }, { label: '更新凭据', value: 'replace' }]" aria-label="重复账号处理策略" /></FormField>
        <NCheckbox v-model="importOptions.prevalidate" aria-label="导入后预验活">导入后预验活</NCheckbox>
      </div>
      <CalloutBox v-if="importMessage" :tone="messageTone(importMessage)" variant="soft" size="sm">{{ importMessage }}</CalloutBox>
      <div v-if="importPreview" class="preview-surface">
        <div class="preview-summary"><span>识别为 {{ importPreview.detected_format }}</span><b>{{ importPreview.parsed_count }} 条</b><span>可导入 {{ importPreview.insertable_count }}</span><span>重复 {{ importPreview.duplicate_count }}</span><span>错误 {{ importPreview.failed_count }}</span></div>
        <TableShell :show-empty="!importPreview.samples.length" :empty-colspan="5" empty-title="暂无预览记录" variant="soft" size="sm"><template #head><tr><th>记录</th><th>账号</th><th>AT</th><th>RT</th><th>结果</th></tr></template><tr v-for="sample in importPreview.samples" :key="sample.locator"><td>{{ sample.locator }}</td><td>{{ sample.email }}</td><td>{{ sample.has_access_token ? sample.access_token_hint : '—' }}</td><td>{{ sample.has_refresh_token ? sample.refresh_token_hint : '—' }}</td><td><StatusPill :label="sample.duplicate ? '重复' : '可导入'" :tone="sample.duplicate ? 'warning' : 'success'" size="xs" radius="rounded" /></td></tr></TableShell>
      </div>
    </div>
    <template #footer><NButton type="button" variant="outline" size="sm" @click="accountImportOpen = false">取消</NButton><NButton type="button" variant="outline" size="sm" :disabled="!importFiles.length || importBusy" @click="previewImport"><LoaderCircle v-if="importBusy" :size="15" class="spin" /><PackageOpen v-else :size="15" />预览</NButton><NButton type="button" variant="primary" size="sm" :disabled="!importPreview || importBusy" @click="commitImport"><Upload :size="15" />确认导入</NButton></template>
  </ModalShell>

  <ModalShell :open="cdkGeneratorOpen" title="生成 CDK" description="设置邮箱类型、生成数量、账号额度和交付文件格式。" max-width="720px" @close="cdkGeneratorOpen = false">
    <div class="generator-dialog-body">
      <div class="cdk-form">
        <FormField label="邮箱类型"><FilterSelect v-model="cdkForm.email_type" :options="cdkEmailTypeOptions" aria-label="CDK 邮箱类型" /></FormField>
        <FormField label="生成数量"><NInput v-model.number="cdkForm.count" type="number" size="sm" block /></FormField>
        <FormField label="账号额度"><NInput v-model.number="cdkForm.quota" type="number" size="sm" block /></FormField>
        <FormField label="交付格式"><FilterSelect v-model="cdkForm.export_format" :options="[{ label: 'JSON', value: 'json' }, { label: 'CSV', value: 'csv' }, { label: 'TXT', value: 'txt' }]" aria-label="CDK 交付格式" /></FormField>
      </div>
      <CalloutBox v-if="cdkMessage" :tone="messageTone(cdkMessage)" variant="soft" size="sm">{{ cdkMessage }}</CalloutBox>
      <div v-if="generatedCodes.length" class="dialog-code-result"><div><span>本次生成 {{ generatedCodes.length }} 个</span><NButton type="button" variant="outline" size="xs" icon-only title="复制本次生成的 CDK" @click="copyCodes"><Clipboard :size="15" /></NButton></div><CodeBlock :code="generatedCodes.join('\n')" /></div>
    </div>
    <template #footer><NButton type="button" variant="outline" size="sm" @click="cdkGeneratorOpen = false">关闭</NButton><NButton type="button" variant="primary" size="sm" :disabled="loading" @click="generateCdks"><LoaderCircle v-if="loading" :size="15" class="spin" /><KeyRound v-else :size="15" />生成 CDK</NButton></template>
  </ModalShell>

  <ConfirmDialog :open="deleteDialog.open" :title="`删除${deleteDialog.label}`" :message="`确认删除选中的 ${deleteDialog.count} 个${deleteDialog.label}吗？已关联或正在执行的数据仍会由系统保留。`" confirm-text="确认删除" cancel-text="取消" @confirm="confirmDelete" @cancel="closeDeleteDialog" />
  <ConfirmDialog :open="proxyGroupDeleteDialog.open" title="删除代理组" :message="`确认删除代理组“${proxyGroupDeleteDialog.name}”吗？组内节点配置也会一并移除。`" confirm-text="确认删除" cancel-text="取消" @confirm="confirmDeleteProxyGroup" @cancel="closeDeleteProxyGroup" />
  <ConfirmDialog :open="reissueDialog.open" title="重新签发 CDK" :message="`确认重新签发 ${reissueDialog.prefix}-**** 吗？原 CDK 将立即失效，新 CDK 会保存加密副本并复制到剪贴板。`" confirm-text="重新签发" cancel-text="取消" @confirm="confirmReissue" @cancel="closeReissueDialog" />
  <Toast :toasts="toasts" @remove="removeToast" />
</template>
