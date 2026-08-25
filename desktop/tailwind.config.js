import typography from '@tailwindcss/typography'

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/renderer/**/*.{html,tsx,ts}'],
  theme: {
    extend: {
      // Theme tokens. Values resolve at runtime to RGB-triplet CSS variables
      // declared in src/renderer/App.css (`:root` = Cream default, plus one
      // `[data-theme=…]` block per theme). The `<alpha-value>` placeholder is
      // what lets Tailwind compile opacity modifiers (`bg-accent/15`,
      // `text-ink-faint/60`, `divide-surface-border/50`, …) correctly.
      colors: {
        accent: {
          DEFAULT: 'rgb(var(--accent) / <alpha-value>)',
          hover: 'rgb(var(--accent-hover) / <alpha-value>)',
          muted: 'rgb(var(--accent-muted) / <alpha-value>)',
          ink: 'rgb(var(--accent-ink) / <alpha-value>)',
        },
        sidebar: {
          DEFAULT: 'rgb(var(--sidebar) / <alpha-value>)',
          hover: 'rgb(var(--sidebar-hover) / <alpha-value>)',
          active: 'rgb(var(--sidebar-active) / <alpha-value>)',
        },
        chat: {
          DEFAULT: 'rgb(var(--chat) / <alpha-value>)',
          user: 'rgb(var(--chat-user) / <alpha-value>)',
          agent: 'rgb(var(--chat-agent) / <alpha-value>)',
        },
        surface: {
          DEFAULT: 'rgb(var(--surface) / <alpha-value>)',
          raised: 'rgb(var(--surface-raised) / <alpha-value>)',
          border: 'rgb(var(--surface-border) / <alpha-value>)',
          input: 'rgb(var(--surface-input) / <alpha-value>)',
        },
        ink: {
          DEFAULT: 'rgb(var(--ink) / <alpha-value>)',
          soft: 'rgb(var(--ink-soft) / <alpha-value>)',
          faint: 'rgb(var(--ink-faint) / <alpha-value>)',
        },
      },
    },
  },
  plugins: [typography],
}
