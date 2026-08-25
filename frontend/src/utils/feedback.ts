/**
 * 问题反馈模块公共工具（v6.1）。
 *
 * 当前包含按任务持久化的重试计数（localStorage）。
 * 确定性预筛 / 诊断报告拼接 / GitHub Issue 链接构造见后续阶段增量。
 */

const RETRY_COUNT_PREFIX = 'fb_retry_'

/**
 * 读取任务的重试次数（未记录或数据异常时返回 0）。
 */
export function getRetryCount(taskId: string): number {
  if (!taskId) return 0
  try {
    const raw = localStorage.getItem(RETRY_COUNT_PREFIX + taskId)
    const n = raw ? parseInt(raw, 10) : 0
    return Number.isFinite(n) && n > 0 ? n : 0
  } catch {
    return 0
  }
}

/**
 * 重试次数 +1 并返回新值。
 */
export function bumpRetryCount(taskId: string): number {
  if (!taskId) return 0
  const next = getRetryCount(taskId) + 1
  try {
    localStorage.setItem(RETRY_COUNT_PREFIX + taskId, String(next))
  } catch {
    /* 存储不可用时静默降级（计数不持久，不影响功能） */
  }
  return next
}

/**
 * 清理任务的重试计数（任务成功后调用）。
 */
export function clearRetryCount(taskId: string): void {
  if (!taskId) return
  try {
    localStorage.removeItem(RETRY_COUNT_PREFIX + taskId)
  } catch {
    /* ignore */
  }
}
