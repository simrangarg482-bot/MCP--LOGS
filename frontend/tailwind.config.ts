import type { Config } from "tailwindcss";

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        display: [
          "Space Grotesk",
          "Inter",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "sans-serif",
        ],
      },
      colors: {
        // Dark "Aurora Glass" theme. Token names are kept identical to the
        // previous light theme so every existing consumer (bg-surface,
        // text-ink, border-border, bg-accent-subtle, …) inherits the new
        // look without a rewrite.
        background: "#0A0B12",
        surface: "#12131C",
        sidebar: {
          DEFAULT: "rgba(15, 17, 27, 0.72)",
          hover: "rgba(255, 255, 255, 0.06)",
          border: "rgba(255, 255, 255, 0.08)",
          text: "#B8BFCF",
          "text-muted": "#6B7280",
        },
        border: {
          DEFAULT: "rgba(255, 255, 255, 0.08)",
          strong: "rgba(255, 255, 255, 0.16)",
        },
        ink: {
          DEFAULT: "#E8EAF2",
          muted: "#98A2B3",
          subtle: "#6B7280",
        },
        accent: {
          DEFAULT: "#5B8DEF",
          hover: "#7BA6F5",
          subtle: "rgba(91, 141, 239, 0.14)",
          border: "rgba(91, 141, 239, 0.35)",
        },
        success: {
          DEFAULT: "#4ADE80",
          subtle: "rgba(74, 222, 128, 0.12)",
          border: "rgba(74, 222, 128, 0.30)",
        },
        warning: {
          DEFAULT: "#FBBF24",
          subtle: "rgba(251, 191, 36, 0.12)",
          border: "rgba(251, 191, 36, 0.30)",
        },
        critical: {
          DEFAULT: "#F87171",
          subtle: "rgba(248, 113, 113, 0.12)",
          border: "rgba(248, 113, 113, 0.30)",
        },
        info: {
          DEFAULT: "#38BDF8",
          subtle: "rgba(56, 189, 248, 0.12)",
          border: "rgba(56, 189, 248, 0.30)",
        },
      },
      boxShadow: {
        subtle: "0 1px 2px 0 rgb(0 0 0 / 0.3)",
        panel: "0 8px 30px -12px rgb(0 0 0 / 0.6)",
        glass:
          "0 8px 32px -8px rgb(0 0 0 / 0.55), inset 0 1px 0 0 rgb(255 255 255 / 0.06)",
        glow: "0 0 0 1px rgb(91 141 239 / 0.25), 0 8px 28px -6px rgb(91 141 239 / 0.45)",
      },
      fontSize: {
        xs: ["0.75rem", { lineHeight: "1rem" }],
        sm: ["0.8125rem", { lineHeight: "1.25rem" }],
        base: ["0.875rem", { lineHeight: "1.375rem" }],
      },
    },
  },
  plugins: [],
} satisfies Config;
