// Shared SSRF rule validator — the renderer half mirrors
// cluxmate/tools/_ssrf.py::parse_entry. The guard silently drops entries it
// cannot parse, so the Settings UI flags anything `parse_entry` would reject:
// an entry shown as saved but never enforced is a security footgun.
//
// Acceptance (mirrors parse_entry):
//   - trimmed non-empty, no whitespace anywhere;
//   - [ipv6] or [ipv6]:port (port 1-65535; a bracketed IPv4 literal and a
//     missing ':' before the port are also accepted, matching Python's
//     _parse_port);
//   - bare IP literal (IPv4 dotted-quad, each octet 0-255; or IPv6 — hex
//     groups / '::' / trailing IPv4-mapped dotted quad);
//   - CIDR ip/prefix where the ip part is a valid IP literal and the prefix is
//     digits 0-32 (IPv4) / 0-128 (IPv6);
//   - host:port (host with no ':', port 1-65535);
//   - otherwise a bare hostname → valid.

const IPv4_RE = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/
const HEX_GROUP_RE = /^[0-9a-f]{1,4}$/
const DIGITS_RE = /^\d+$/

function isIpv4(s: string): boolean {
  return IPv4_RE.test(s)
}

// Structural IPv6 check (mirrors Python's ipaddress.ip_address): 8 hex groups,
// at most one '::' (which covers ≥ 1 zero group), the last 32 bits may be an
// IPv4 dotted quad (counts as two groups), and an optional trailing %zone
// (Python 3.9+ scoped addresses; the zone may contain any char except '%',
// '/' or whitespace — mirroring ipaddress' scope handling).
function isIpv6(s: string): boolean {
  if (!s.includes(':')) return false
  let body = s
  const pct = s.indexOf('%')
  if (pct !== -1) {
    if (s.indexOf('%', pct + 1) !== -1) return false
    const zone = s.slice(pct + 1)
    if (!zone || zone.includes('/') || /\s/.test(zone)) return false
    body = s.slice(0, pct)
  }
  if (!/^[0-9a-f:.]+$/.test(body)) return false
  const lastColon = body.lastIndexOf(':')
  const tailPart = body.slice(lastColon + 1)
  const hasIpv4Tail = tailPart.includes('.')
  if (hasIpv4Tail) {
    if (!isIpv4(tailPart)) return false
  }
  const head = hasIpv4Tail ? body.slice(0, lastColon + 1) : body
  const doubleColons = head.split('::').length - 1
  if (doubleColons > 1) return false
  const parts = head.split(/::|:/).filter(Boolean)
  for (const g of parts) {
    if (!HEX_GROUP_RE.test(g)) return false
  }
  const groups = parts.length + (hasIpv4Tail ? 2 : 0)
  return doubleColons === 1 ? groups <= 7 : groups === 8
}

function isIpLiteral(s: string): boolean {
  return isIpv4(s) || isIpv6(s)
}

// Mirrors Python's _parse_port: an optional leading ':' plus a 1-65535 port.
function parsePort(text: string): number | null {
  if (!text) return null
  const t = text[0] === ':' ? text.slice(1) : text
  if (!DIGITS_RE.test(t)) return null
  const n = Number(t)
  return n >= 1 && n <= 65535 ? n : null
}

export function isValidSsrEntry(entry: string): boolean {
  const raw = (entry ?? '').trim()
  if (!raw) return false
  const low = raw.toLowerCase()
  if (/\s/.test(low)) return false
  // [ipv6] / [ipv6]:port (parse_entry also accepts a bracketed IPv4 literal
  // and a port written without the ':' — mirror it so no false warnings).
  if (low.startsWith('[')) {
    const close = low.indexOf(']')
    if (close === -1) return false
    if (!isIpLiteral(low.slice(1, close))) return false
    const rest = low.slice(close + 1)
    if (rest === '') return true
    return parsePort(rest) !== null
  }
  // Bare IP literal (IPv4 dotted-quad, or IPv6).
  if (isIpLiteral(low)) return true
  // CIDR ip/prefix.
  if (low.includes('/')) {
    const slash = low.indexOf('/')
    if (low.indexOf('/', slash + 1) !== -1) return false
    const ip = low.slice(0, slash)
    const prefix = low.slice(slash + 1)
    if (!DIGITS_RE.test(prefix)) return false
    const p = Number(prefix)
    if (isIpv4(ip)) return p <= 32
    if (isIpv6(ip)) return p <= 128
    return false
  }
  // host:port — the host part must not contain another ':'.
  if (low.includes(':')) {
    const idx = low.lastIndexOf(':')
    const hostPart = low.slice(0, idx)
    const port = parsePort(low.slice(idx + 1))
    return port !== null && hostPart.length > 0 && !hostPart.includes(':')
  }
  // Bare hostname → accepted.
  return true
}
