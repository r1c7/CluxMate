// Format a time source (ISO string or Unix ms) to a human-readable string.
// - Today:          HH:mm            (e.g. "14:30")
// - This year:      MMM D HH:mm      (e.g. "Jul 27 14:30")
// - Other years:    MMM D, YYYY HH:mm (e.g. "Jul 27, 2025 14:30")
export function formatTime(source: string | number): string {
  const date = typeof source === 'string' ? new Date(source) : new Date(source)
  const now = new Date()
  const isToday =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  const isThisYear = date.getFullYear() === now.getFullYear()

  const hh = String(date.getHours()).padStart(2, '0')
  const mm = String(date.getMinutes()).padStart(2, '0')
  const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

  if (isToday) return `${hh}:${mm}`
  if (isThisYear) return `${monthNames[date.getMonth()]} ${date.getDate()} ${hh}:${mm}`
  return `${monthNames[date.getMonth()]} ${date.getDate()}, ${date.getFullYear()} ${hh}:${mm}`
}
