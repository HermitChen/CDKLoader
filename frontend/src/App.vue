<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  Archive,
  ChevronLeft,
  ChevronRight,
  Clipboard,
  CloudDownload,
  Download,
  FileUp,
  Gauge,
  Github,
  KeyRound,
  LayoutDashboard,
  ListChecks,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  PackageOpen,
  RefreshCw,
  Search,
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
  TableShell,
  Toast,
  ToolbarShell,
} from 'nanocat-ui'
import { adminHeaders, ApiError, download, request } from './api'

const isAdmin = ref(window.location.pathname.startsWith('/admin'))
const currentYear = new Date().getFullYear()
const adminToken = ref(localStorage.getItem('cdk_loader_admin_token') || '')
const loginPassword = ref('')
const loginError = ref('')
const activeView = ref('overview')
const loading = ref(false)
const accountExportBusy = ref(false)
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
const accountFilters = ref({ q: '', status: '', has_refresh_token: '', cdk_id: '', redemption_id: '', relation_label: '' })
const cdkFilters = ref({ q: '', quota: '', status: '' })
const redemptionFilters = ref({ q: '', status: '', today: false })
const selectedAccountIds = ref([])
const selectedCdkIds = ref([])
const selectedRedemptionIds = ref([])
const selectionBusy = ref('')
const accountMessage = ref('')
const redemptionMessage = ref('')
const toasts = ref([])
const deleteDialog = ref({ open: false, kind: '', count: 0, label: '' })
const reissueDialog = ref({ open: false, id: '', prefix: '' })
const accountImportOpen = ref(false)
const cdkGeneratorOpen = ref(false)

const importFile = ref(null)
const importInput = ref(null)
const importPreview = ref(null)
const importBusy = ref(false)
const importMessage = ref('')
const importOptions = ref({ duplicate_strategy: 'skip', prevalidate: true })

const cdkForm = ref({ count: 1, quota: 1, export_format: 'json' })
const generatedCodes = ref([])
const cdkMessage = ref('')
const cdkFilterMessage = ref('')

const codeInput = ref('')
const redeemBusy = ref(false)
const redeemError = ref('')
const redeemState = ref(null)
const redeemStage = ref('')

const navItems = [
  { id: 'overview', label: '概览', icon: LayoutDashboard },
  { id: 'accounts', label: '账号池', icon: Users },
  { id: 'cdks', label: 'CDK', icon: KeyRound },
  { id: 'redemptions', label: '兑换记录', icon: Archive },
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
const cdkStatusOptions = [
  { label: '全部状态', value: '' }, { label: '未使用', value: 'unused' },
  { label: '部分使用', value: 'partial' }, { label: '已耗尽', value: 'exhausted' },
  { label: '已过期', value: 'expired' }, { label: '已禁用', value: 'disabled' },
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
}

const publicCodes = computed(() => codeInput.value.split(/[\n,]+/).map((value) => value.trim()).filter(Boolean))
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
  if (['available', 'completed', 'unused', 'validating', 'redelivery_ready', 'redelivery_completed'].includes(value)) return 'success'
  if (['reserved', 'partial', 'processing', 'queued', 'pending_validation'].includes(value)) return 'warning'
  if (['expired', 'banned', 'failed', 'exhausted', 'disabled'].includes(value)) return 'error'
  return 'neutral'
}

function statusLabel(value) {
  return {
    available: '可用', reserved: '已预约', delivered: '已交付', pending_validation: '待验活',
    quarantined: '已隔离', expired: '已失效', banned: '已封禁', completed: '已完成',
    processing: '处理中', queued: '排队中', failed: '失败', unused: '未使用', partial: '部分使用',
    exhausted: '已耗尽', disabled: '已禁用', validating: '验活中',
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
    const [dashboardResult] = await Promise.all([
      adminRequest('/admin/dashboard'),
      loadAccounts(),
      loadCdks(),
      loadRedemptions(),
    ])
    dashboard.value = dashboardResult
  } catch (error) {
    if (error instanceof ApiError && /认证/.test(error.message)) logout()
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

async function exportSelectedAccounts() {
  await exportAccounts(selectedAccountIds.value, '导出完成')
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

function openTodayRedemptions() {
  activeView.value = 'redemptions'
  redemptionFilters.value.today = true
  redemptionPage.value = 1
  loadRedemptions({ clearSelection: true })
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
    cdk_id: cdk.id,
    redemption_id: '',
    relation_label: `CDK：${cdkLabel(cdk)}`,
  }
  accountPage.value = 1
  loadAccounts({ clearSelection: true })
}

function openAccountsForRedemption(redemption) {
  activeView.value = 'accounts'
  accountFilters.value = {
    q: '',
    status: '',
    has_refresh_token: '',
    cdk_id: '',
    redemption_id: redemption.id,
    relation_label: `兑换任务：${redemption.id.slice(0, 8)}`,
  }
  accountPage.value = 1
  loadAccounts({ clearSelection: true })
}

function clearAccountRelationFilter() {
  accountFilters.value.cdk_id = ''
  accountFilters.value.redemption_id = ''
  accountFilters.value.relation_label = ''
  accountPage.value = 1
  loadAccounts({ clearSelection: true })
}

function openCdkForAccount(account) {
  const cdk = account.related_cdk
  if (!cdk) return
  activeView.value = 'cdks'
  cdkFilters.value = { q: cdk.code || cdk.prefix, quota: '', status: '' }
  cdkPage.value = 1
  loadCdks({ clearSelection: true })
}

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
  importInput.value?.click()
}

function setImportFile(file) {
  if (!file) return
  importFile.value = file
  importPreview.value = null
  importMessage.value = ''
}

function onImportFileChange(event) {
  setImportFile(event.target.files?.[0])
}

function onDrop(event) {
  event.preventDefault()
  setImportFile(event.dataTransfer.files?.[0])
}

async function previewImport() {
  if (!importFile.value) return
  importBusy.value = true
  importMessage.value = ''
  try {
    const form = new FormData()
    form.append('file', importFile.value)
    importPreview.value = await adminRequest('/admin/account-imports/preview', { method: 'POST', body: form })
  } catch (error) {
    importMessage.value = error.message
    pushToast('error', '预览失败', error.message)
  } finally {
    importBusy.value = false
  }
}

async function commitImport() {
  if (!importFile.value) return
  importBusy.value = true
  importMessage.value = ''
  try {
    const form = new FormData()
    form.append('file', importFile.value)
    form.append('duplicate_strategy', importOptions.value.duplicate_strategy)
    form.append('prevalidate', String(importOptions.value.prevalidate))
    const result = await adminRequest('/admin/account-imports', { method: 'POST', body: form })
    importMessage.value = result.status === 'validating' ? '导入已提交，正在预验活。' : '导入已完成。'
    pushToast('success', '账号导入', importMessage.value)
    importFile.value = null
    importPreview.value = null
    accountImportOpen.value = false
    if (importInput.value) importInput.value.value = ''
    await loadAdminData()
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
  for (let attempt = 0; attempt < 50; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 700))
    const result = await request(`/redemptions/${id}?token=${encodeURIComponent(token)}`)
    redeemState.value = result
    if (result.status === 'completed') {
      redeemStage.value = '正在准备下载'
      await download(`/redemptions/${id}/download?token=${encodeURIComponent(token)}`)
      return
    }
    if (result.status === 'failed') throw new ApiError(result.error_message || '兑换失败')
  }
  throw new ApiError('任务等待超时，请稍后使用原任务凭证查询。')
}

async function redeem() {
  if (!publicCodes.value.length || redeemBusy.value) return
  redeemBusy.value = true
  redeemError.value = ''
  redeemState.value = null
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
      redeemStage.value = result.message || 'CDK 已兑换，正在补发关联账号'
      await download(`/redeliveries/${result.id}/download?token=${encodeURIComponent(result.task_token)}`)
      redeemState.value = { ...result, status: 'redelivery_completed' }
      return
    }
    if (result.status === 'completed') {
      redeemStage.value = '正在准备下载'
      await download(`/redemptions/${result.id}/download?token=${encodeURIComponent(result.task_token)}`)
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
  if (isAdmin.value && adminToken.value) await loadAdminData()
})
</script>

<template>
  <div v-if="!isAdmin" class="public-page">
    <main class="redeem-shell">
      <div class="redeem-brand"><span class="brand-icon"><KeyRound :size="17" /></span><span>CDK Loader</span></div>
      <section class="redeem-title" aria-labelledby="redeem-heading">
        <p class="eyebrow">PUBLIC EXTRACTION</p>
        <h1 id="redeem-heading">CDK 文件提取</h1>
        <p>输入有效提取码，验证通过后获取账号文件。</p>
      </section>

      <FormSection class="redeem-tool" title="输入 CDK 提取码" description="每行一个 CDK，也支持逗号分隔。" variant="outline" size="md" body-class="redeem-body">
        <textarea id="cdk-input" v-model="codeInput" :disabled="redeemBusy" placeholder="CDK-XXXX-XXXX-XXXX-XXXX&#10;CDK-YYYY-YYYY-YYYY-YYYY" spellcheck="false" />
        <div class="redeem-meta"><span>{{ publicCodes.length ? `已识别 ${publicCodes.length} 个 CDK` : '等待输入' }}</span><span v-if="redeemBusy" class="processing"><LoaderCircle :size="15" class="spin" />{{ redeemStage || publicStatusText }}</span></div>
        <CalloutBox v-if="redeemError" tone="error" variant="soft" size="sm">{{ redeemError }}</CalloutBox>
        <div v-if="redeemState && !redeemError" class="redeem-result"><StatusPill :label="publicStatusText" :tone="statusTone(publicStatus)" size="xs" radius="rounded" /><span v-if="redeemState.delivered_count">{{ redeemState.delivered_count }} 个账号</span></div>
        <NButton class="redeem-action" type="button" variant="primary" size="md" block :disabled="!publicCodes.length || redeemBusy" @click="redeem"><CloudDownload v-if="!redeemBusy" :size="17" /><LoaderCircle v-else :size="17" class="spin" />{{ redeemBusy ? '处理中' : '下载 JSON 包' }}</NButton>
      </FormSection>
    </main>
    <footer class="public-footer">
      <span>&copy; {{ currentYear }} CDK Loader. All rights reserved.</span>
      <a href="https://github.com/HermitChen/CDKLoader" target="_blank" rel="noopener noreferrer" aria-label="在 GitHub 查看 CDK Loader" title="GitHub">
        <Github :size="17" />
      </a>
    </footer>
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
        <div class="header-actions"><NButton v-if="activeView === 'accounts'" type="button" variant="outline" size="sm" :disabled="accountExportBusy || !selectedAccountIds.length" @click="exportSelectedAccounts"><LoaderCircle v-if="accountExportBusy" :size="15" class="spin" /><Download v-else :size="15" />导出选中</NButton><NButton v-if="activeView === 'accounts'" type="button" variant="primary" size="sm" @click="openAccountImport"><Upload :size="15" />导入账号</NButton><NButton v-if="activeView === 'cdks'" type="button" variant="primary" size="sm" @click="openCdkGenerator"><KeyRound :size="15" />生成 CDK</NButton><NButton class="refresh-button" icon-only variant="outline" size="sm" type="button" title="刷新数据" @click="loadAdminData"><RefreshCw :size="16" /></NButton></div>
      </header>

      <section v-if="activeView === 'overview'" class="overview-view">
        <div class="overview-grid">
          <StatCard label="可用账号" :value="dashboard?.accounts.available ?? '-'" caption="可预约交付" icon="lucide:users" icon-tone="success" />
          <StatCard label="预约中" :value="dashboard?.accounts.reserved ?? '-'" caption="兑换处理中" icon="lucide:clock-3" icon-tone="warning" />
          <StatCard label="隔离账号" :value="dashboard?.accounts.quarantined ?? '-'" caption="等待后续确认" icon="lucide:shield-alert" icon-tone="error" />
          <StatCard label="剩余额度" :value="dashboard?.cdk_remaining_quota ?? '-'" caption="全部 CDK" icon="lucide:key-round" icon-tone="info" />
        </div>
        <FormSection class="system-panel" title="运行状态" description="当前服务运行配置和今日活动。" variant="outline">
          <div class="system-row"><span class="system-label"><Gauge :size="16" />验活模式</span><StatusPill :label="dashboard?.validation_mode || '—'" tone="info" size="xs" radius="rounded" /><span class="system-divider" aria-hidden="true" /><button class="today-redemptions" type="button" title="查看今日兑换记录" @click="openTodayRedemptions"><span class="system-label">今日兑换</span><strong>{{ dashboard?.today_redemptions ?? '-' }}</strong><ChevronRight :size="15" /></button></div>
        </FormSection>
      </section>

      <section v-if="activeView === 'accounts'" class="workspace-section">
        <ToolbarShell class="list-toolbar" stack-on-mobile>
          <template #start><div class="selection-actions"><div class="list-summary"><span>当前结果</span><strong>{{ accountTotal }}</strong><span>个账号</span></div><NButton v-if="accountTotal > accounts.length" type="button" variant="outline" size="xs" :disabled="Boolean(selectionBusy)" @click="toggleAllResults('accounts')"><LoaderCircle v-if="selectionBusy === 'accounts'" :size="15" class="spin" /><ListChecks v-else :size="15" />{{ allAccountResultsSelected ? '取消全部选择' : `选择全部 ${accountTotal} 项` }}</NButton><span v-if="selectedAccountIds.length" class="selection-count">已选择 {{ selectedAccountIds.length }} 项</span><NButton v-if="selectedAccountIds.length" type="button" variant="danger" size="xs" icon-only title="删除选中账号" @click="openDeleteDialog('accounts')"><Trash2 :size="15" /></NButton></div></template>
          <template #end><div class="filter-group"><span v-if="accountFilters.relation_label" class="relation-filter"><span :title="accountFilters.relation_label">{{ accountFilters.relation_label }}</span><NButton type="button" variant="outline" size="xs" icon-only title="清除关联筛选" @click="clearAccountRelationFilter"><X :size="14" /></NButton></span><div class="search-field"><Search :size="15" /><NInput v-model="accountFilters.q" size="sm" placeholder="搜索账号、来源或 ID" aria-label="搜索账号" @keyup.enter="searchAccounts" /></div><FilterSelect v-model="accountFilters.has_refresh_token" :options="refreshTokenOptions" size="sm" aria-label="Refresh Token 筛选" @update:model-value="searchAccounts" /><FilterSelect v-model="accountFilters.status" :options="accountStatusOptions" size="sm" aria-label="账号状态筛选" @update:model-value="searchAccounts" /></div></template>
        </ToolbarShell>
        <CalloutBox v-if="accountMessage" :tone="messageTone(accountMessage)" variant="soft" size="sm" class="section-message">{{ accountMessage }}</CalloutBox>
        <TableShell class="data-table accounts-table" :show-empty="!accounts.length" :empty-colspan="8" empty-title="暂无账号" empty-description="导入账号后会显示在这里。" variant="soft" size="sm"><template #head><tr><th class="selection-cell"><NCheckbox :model-value="allAccountsSelected" :indeterminate="selectedAccountsOnPage > 0 && !allAccountsSelected" aria-label="选择当前页账号" @update:model-value="toggleAllAccounts" /></th><th>账号</th><th>关联 CDK</th><th>来源</th><th>凭据</th><th>时间（东八区）</th><th>状态</th><th class="action-cell">操作</th></tr></template><tr v-for="account in accounts" :key="account.id"><td class="selection-cell"><NCheckbox :model-value="selectedAccountIds.includes(account.id)" :aria-label="`选择 ${account.email}`" @update:model-value="toggleAccount(account.id, $event)" /></td><td><b>{{ account.email || '—' }}</b><small>{{ account.account_id || '—' }}</small></td><td class="relation-cell"><button v-if="account.related_cdk" class="relation-code" type="button" :title="`查看 ${cdkLabel(account.related_cdk)}`" @click="openCdkForAccount(account)">{{ cdkLabel(account.related_cdk) }}</button><small v-if="account.related_cdk">任务 {{ account.related_cdk.redemption_id.slice(0, 8) }}</small><span v-else>—</span></td><td>{{ account.source }}</td><td>{{ account.has_access_token ? 'AT' : '' }}{{ account.has_refresh_token ? ' · RT' : '' }}</td><td><b>{{ formatDate(account.delivered_at || account.validated_at) }}</b><small>{{ account.delivered_at ? '交付时间' : '验活时间' }}</small></td><td><StatusPill :label="statusLabel(account.status)" :tone="statusTone(account.status)" size="xs" radius="rounded" /></td><td class="action-cell"><NButton v-if="account.related_cdk" type="button" variant="outline" size="xs" icon-only title="二次导出账号" :disabled="accountExportBusy" @click="reexportAccount(account)"><LoaderCircle v-if="accountExportBusy" :size="15" class="spin" /><Download v-else :size="15" /></NButton><span v-else>—</span></td></tr></TableShell>
        <div v-if="accountTotal" class="pagination-bar" aria-label="账号池分页"><span>{{ pageRange(accountTotal, accountPage, accountPageSize) }}</span><div class="pagination-controls"><FilterSelect class="page-size-select" v-model="accountPageSize" :options="pageSizeOptions" size="sm" aria-label="账号池每页条数" @update:model-value="changeAccountPageSize" /><NButton type="button" variant="outline" size="xs" icon-only title="上一页" :disabled="accountPage <= 1" @click="changeAccountPage(-1)"><ChevronLeft :size="15" /></NButton><strong>{{ accountPage }} / {{ pageCount(accountTotal, accountPageSize) }}</strong><NButton type="button" variant="outline" size="xs" icon-only title="下一页" :disabled="accountPage >= pageCount(accountTotal, accountPageSize)" @click="changeAccountPage(1)"><ChevronRight :size="15" /></NButton></div></div>
      </section>

      <section v-if="activeView === 'cdks'" class="workspace-section">
        <ToolbarShell class="list-toolbar" stack-on-mobile>
          <template #start><div class="selection-actions"><div class="list-summary"><span>当前结果</span><strong>{{ cdkTotal }}</strong><span>个 CDK</span></div><NButton v-if="cdkTotal > cdks.length" type="button" variant="outline" size="xs" :disabled="Boolean(selectionBusy)" @click="toggleAllResults('cdks')"><LoaderCircle v-if="selectionBusy === 'cdks'" :size="15" class="spin" /><ListChecks v-else :size="15" />{{ allCdkResultsSelected ? '取消全部选择' : `选择全部 ${cdkTotal} 项` }}</NButton><span v-if="selectedCdkIds.length" class="selection-count">已选择 {{ selectedCdkIds.length }} 项</span><NButton v-if="selectedCdkIds.length" type="button" variant="outline" size="xs" icon-only title="复制选中 CDK" @click="copySelectedCdks"><Clipboard :size="15" /></NButton><NButton v-if="selectedCdkIds.length" type="button" variant="danger" size="xs" icon-only title="删除选中 CDK" @click="openDeleteDialog('cdks')"><Trash2 :size="15" /></NButton></div></template>
          <template #end><div class="filter-group"><div class="search-field"><Search :size="15" /><NInput v-model="cdkFilters.q" size="sm" placeholder="搜索完整 CDK 或前缀" aria-label="搜索 CDK" @keyup.enter="searchCdks" /></div><div class="quota-search-field"><Gauge :size="15" /><NInput v-model="cdkFilters.quota" size="sm" placeholder="额度：10 或 0/10" aria-label="按额度筛选 CDK" @keyup.enter="searchCdks" /></div><FilterSelect v-model="cdkFilters.status" :options="cdkStatusOptions" size="sm" aria-label="CDK 状态筛选" @update:model-value="searchCdks" /></div></template>
        </ToolbarShell>
        <CalloutBox v-if="cdkFilterMessage" tone="warning" variant="soft" size="sm" class="section-message">{{ cdkFilterMessage }}</CalloutBox>
        <TableShell class="data-table cdks-table" :show-empty="!cdks.length" :empty-colspan="9" empty-title="暂无 CDK" empty-description="生成 CDK 后会显示在这里。" variant="soft" size="sm"><template #head><tr><th class="selection-cell"><NCheckbox :model-value="allCdksSelected" :indeterminate="selectedCdksOnPage > 0 && !allCdksSelected" aria-label="选择当前页 CDK" @update:model-value="toggleAllCdks" /></th><th>CDK</th><th>剩余 / 总额度</th><th>冻结</th><th>格式</th><th>有效期</th><th>已兑换账号</th><th>状态</th><th class="action-cell">操作</th></tr></template><tr v-for="cdk in cdks" :key="cdk.id"><td class="selection-cell"><NCheckbox :model-value="selectedCdkIds.includes(cdk.id)" :aria-label="`选择 ${cdk.prefix}`" @update:model-value="toggleCdk(cdk.id, $event)" /></td><td><b>{{ cdkLabel(cdk) }}</b><small v-if="!cdk.code">历史明文不可恢复</small></td><td>{{ cdk.remaining_quota }} / {{ cdk.total_quota }}</td><td>{{ cdk.reserved_quota }}</td><td>{{ cdk.export_format.toUpperCase() }}</td><td>{{ cdk.expires_at ? formatDate(cdk.expires_at, 'date') : '长期' }}</td><td class="relation-cell"><button v-if="cdk.delivery_count" class="relation-count" type="button" :title="`查看 ${cdkLabel(cdk)} 关联的账号`" @click="openAccountsForCdk(cdk)"><Users :size="14" /><span>{{ cdk.delivery_count }} 个账号</span></button><span v-else>—</span></td><td><StatusPill :label="statusLabel(cdk.status)" :tone="statusTone(cdk.status)" size="xs" radius="rounded" /></td><td class="action-cell"><NButton v-if="cdk.can_copy" type="button" variant="outline" size="xs" icon-only title="复制完整 CDK" @click="copySingleCdk(cdk.id)"><Clipboard :size="15" /></NButton><NButton v-else-if="cdk.status === 'unused' && cdk.remaining_quota === cdk.total_quota && cdk.reserved_quota === 0" type="button" variant="outline" size="xs" icon-only title="重新签发并复制" @click="openReissueDialog(cdk)"><RefreshCw :size="15" /></NButton><span v-else>—</span></td></tr></TableShell>
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
    </main>
  </div>

  <ModalShell :open="accountImportOpen" title="导入账号" description="上传账号文件，预览无误后写入账号池。" max-width="880px" @close="accountImportOpen = false">
    <div class="import-dialog-body">
      <div class="dropzone" :class="{ occupied: importFile }" @dragover.prevent @drop="onDrop" @click="chooseImportFile">
        <input ref="importInput" type="file" accept=".json,.csv,.txt,.zip" hidden @change="onImportFileChange" />
        <FileUp :size="24" />
        <strong>{{ importFile ? importFile.name : '选择账号文件' }}</strong>
        <span>{{ importFile ? `${Math.ceil(importFile.size / 1024)} KB` : 'JSON · CSV · TXT · ZIP' }}</span>
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
    <template #footer><NButton type="button" variant="outline" size="sm" @click="accountImportOpen = false">取消</NButton><NButton type="button" variant="outline" size="sm" :disabled="!importFile || importBusy" @click="previewImport"><LoaderCircle v-if="importBusy" :size="15" class="spin" /><PackageOpen v-else :size="15" />预览</NButton><NButton type="button" variant="primary" size="sm" :disabled="!importPreview || importBusy" @click="commitImport"><Upload :size="15" />确认导入</NButton></template>
  </ModalShell>

  <ModalShell :open="cdkGeneratorOpen" title="生成 CDK" description="设置生成数量、账号额度和交付文件格式。" max-width="720px" @close="cdkGeneratorOpen = false">
    <div class="generator-dialog-body">
      <div class="cdk-form">
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
  <ConfirmDialog :open="reissueDialog.open" title="重新签发 CDK" :message="`确认重新签发 ${reissueDialog.prefix}-**** 吗？原 CDK 将立即失效，新 CDK 会保存加密副本并复制到剪贴板。`" confirm-text="重新签发" cancel-text="取消" @confirm="confirmReissue" @cancel="closeReissueDialog" />
  <Toast :toasts="toasts" @remove="removeToast" />
</template>
