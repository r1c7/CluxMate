// build-icon.mjs — regenerate desktop/resources/icon.png and icon.ico from a
// source PNG (default: <repo-root>/C0DE.png). Pure Node (zlib + manual PNG
// codec), no external deps.
//
// The ICO is emitted with PNG-compressed entries (BITMAPINFOHEADER-less,
// supported by Windows Vista+, Electron nativeImage, and electron-builder's
// app-builder). Sizes: 16, 24, 32, 48, 64, 128, 256 — plus the full-res source
// downscaled to 256 as the largest entry.
//
// Usage:
//   node scripts/build-icon.mjs [source.png]
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { deflateSync, inflateSync } from 'node:zlib'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const desktopRoot = resolve(__dirname, '..')
const repoRoot = resolve(desktopRoot, '..')
const src = resolve(repoRoot, process.argv[2] ?? 'C0DE.png')

// ---- minimal PNG decoder ---------------------------------------------------
const PNG_SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

function decodePNG(buf) {
  if (!buf.subarray(0, 8).equals(PNG_SIG)) throw new Error('not a PNG')
  let offset = 8
  let width = 0, height = 0, bitDepth = 0, colorType = 0
  const idat = []
  let palette = null
  while (offset < buf.length) {
    const len = buf.readUInt32BE(offset)
    const type = buf.toString('ascii', offset + 4, offset + 8)
    const data = buf.subarray(offset + 8, offset + 8 + len)
    offset += 12 + len
    if (type === 'IHDR') {
      width = data.readUInt32BE(0)
      height = data.readUInt32BE(4)
      bitDepth = data[8]
      colorType = data[9]
      if (bitDepth !== 8 || (colorType !== 6 && colorType !== 2)) {
        throw new Error(`unsupported PNG format: bitDepth=${bitDepth} colorType=${colorType}`)
      }
    } else if (type === 'PLTE') {
      palette = data
    } else if (type === 'IDAT') {
      idat.push(data)
    } else if (type === 'IEND') {
      break
    }
  }
  const raw = inflateSync(Buffer.concat(idat))
  const channels = colorType === 6 ? 4 : 3
  const stride = width * channels
  const out = Buffer.alloc(width * height * 4)
  const paeth = (a, b, c) => {
    const p = a + b - c
    const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c)
    return pa <= pb && pa <= pc ? a : pb <= pc ? b : c
  }
  let pos = 0
  for (let y = 0; y < height; y++) {
    const filter = raw[pos++]
    const row = raw.subarray(pos, pos + stride)
    pos += stride
    const prev = out.subarray((y - 1) * width * 4, y * width * 4)
    for (let x = 0; x < stride; x++) {
      const a = x >= channels ? row[x - channels] : 0
      const b = y > 0 ? prev[x] : 0
      const c = x >= channels && y > 0 ? prev[x - channels] : 0
      let v = row[x]
      switch (filter) {
        case 1: v = (v + a) & 0xff; break
        case 2: v = (v + b) & 0xff; break
        case 3: v = (v + ((a + b) >> 1)) & 0xff; break
        case 4: v = (v + paeth(a, b, c)) & 0xff; break
        default: break
      }
      row[x] = v
    }
    let srcIdx = 0
    let dstIdx = y * width * 4
    if (channels === 4) {
      row.copy(out, dstIdx)
    } else {
      // RGB → RGBA
      for (let x = 0; x < width; x++) {
        out[dstIdx] = row[srcIdx]
        out[dstIdx + 1] = row[srcIdx + 1]
        out[dstIdx + 2] = row[srcIdx + 2]
        out[dstIdx + 3] = 255
        srcIdx += 3
        dstIdx += 4
      }
    }
  }
  return { width, height, rgba: out }
}

// ---- PNG encoder (always RGBA, filter 0) -----------------------------------
const CRC_TABLE = (() => {
  const t = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c >>> 0
  }
  return t
})()
function crc32(buf) {
  let c = 0xffffffff
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}
function chunk(type, data) {
  const out = Buffer.alloc(12 + data.length)
  out.writeUInt32BE(data.length, 0)
  out.write(type, 4, 'ascii')
  data.copy(out, 8)
  out.writeUInt32BE(crc32(Buffer.concat([Buffer.from(type, 'ascii'), data])), 8 + data.length)
  return out
}
function encodePNG(width, height, rgba) {
  const raw = Buffer.alloc((width * 4 + 1) * height)
  for (let y = 0; y < height; y++) {
    raw[y * (width * 4 + 1)] = 0
    rgba.copy(raw, y * (width * 4 + 1) + 1, y * width * 4, (y + 1) * width * 4)
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 6 // RGBA
  return Buffer.concat([
    PNG_SIG,
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

// ---- box-filter downscale (area average) -----------------------------------
function resize(rgba, sw, sh, dw, dh) {
  const out = Buffer.alloc(dw * dh * 4)
  const xr = sw / dw, yr = sh / dh
  for (let y = 0; y < dh; y++) {
    const y0 = Math.floor(y * yr), y1 = Math.max(y0 + 1, Math.min(sh, Math.ceil((y + 1) * yr)))
    for (let x = 0; x < dw; x++) {
      const x0 = Math.floor(x * xr), x1 = Math.max(x0 + 1, Math.min(sw, Math.ceil((x + 1) * xr)))
      let r = 0, g = 0, b = 0, a = 0, n = 0
      for (let yy = y0; yy < y1; yy++) {
        for (let xx = x0; xx < x1; xx++) {
          const i = (yy * sw + xx) * 4
          r += rgba[i]; g += rgba[i + 1]; b += rgba[i + 2]; a += rgba[i + 3]; n++
        }
      }
      const o = (y * dw + x) * 4
      out[o] = Math.round(r / n)
      out[o + 1] = Math.round(g / n)
      out[o + 2] = Math.round(b / n)
      out[o + 3] = Math.round(a / n)
    }
  }
  return out
}

// ---- ICO writer (PNG-compressed entries) -----------------------------------
function buildICO(pngs) {
  const count = pngs.length
  const header = Buffer.alloc(6)
  header.writeUInt16LE(0, 0) // reserved
  header.writeUInt16LE(1, 2) // type: icon
  header.writeUInt16LE(count, 4)
  const entries = []
  let dataOffset = 6 + 16 * count
  const dir = []
  const blobs = []
  for (const { size, data } of pngs) {
    const e = Buffer.alloc(16)
    e[0] = size >= 256 ? 0 : size // width (0 == 256)
    e[1] = size >= 256 ? 0 : size // height
    e[2] = 0 // palette colors
    e[3] = 0 // reserved
    e.writeUInt16LE(1, 4) // color planes
    e.writeUInt16LE(32, 6) // bits per pixel
    e.writeUInt32LE(data.length, 8) // bytes in resource
    e.writeUInt32LE(dataOffset, 12)
    dir.push(e)
    blobs.push(data)
    dataOffset += data.length
  }
  return Buffer.concat([header, ...dir, ...blobs])
}

// ---- main ------------------------------------------------------------------
const decoded = decodePNG(readFileSync(src))
console.log(`source: ${src} (${decoded.width}x${decoded.height})`)

// Canonical raster copy (full source resolution).
const iconPngPath = join(desktopRoot, 'resources', 'icon.png')
writeFileSync(iconPngPath, readFileSync(src))
console.log(`wrote ${iconPngPath}`)

// ICO entries: standard Windows sizes, capped at 256 (source is 800x800).
const sizes = [16, 24, 32, 48, 64, 128, 256]
const pngs = sizes.map((size) => ({
  size,
  data: encodePNG(size, size, resize(decoded.rgba, decoded.width, decoded.height, size, size)),
}))

const ico = buildICO(pngs)
const icoPath = join(desktopRoot, 'resources', 'icon.ico')
writeFileSync(icoPath, ico)
console.log(`wrote ${icoPath} (${ico.length} bytes, ${pngs.length} entries: ${sizes.join(', ')})`)
