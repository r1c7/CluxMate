import { useCallback } from 'react'
import { useStore } from './stores'
import { t, type Lang } from './i18n'

// Component-facing translate function. Re-renders the consuming component
// whenever the store's `lang` changes, and the returned function identity is
// stable per language (so it's safe as a memo/dependency).
export function useT() {
  const lang = useStore((s: { lang: Lang }) => s.lang)
  return useCallback(
    (key: string, vars?: Record<string, string | number>) => t(lang, key, vars),
    [lang],
  )
}
