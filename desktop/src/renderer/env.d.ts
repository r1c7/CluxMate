// Asset module declarations for the renderer's Vite build. Importing an .svg
// yields its bundled URL (used by the custom title bar's app icon).
declare module '*.svg' {
  const src: string
  export default src
}
