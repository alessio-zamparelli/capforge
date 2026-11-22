/** @type {import('tailwindcss').Config} */
// Tailwind 4 detects sources automatically (respecting .gitignore) and its
// `dark:` variant already follows prefers-color-scheme, so neither `content`
// nor `darkMode` is needed here: only the shadcn-style zinc palette remains.
export default {
  theme: {
    extend: {
      colors: {
        // shadcn-style zinc palette, used via CSS variables below.
        border: "hsl(var(--border))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        muted: "hsl(var(--muted))",
        "muted-foreground": "hsl(var(--muted-foreground))",
        card: "hsl(var(--card))",
        "card-foreground": "hsl(var(--card-foreground))",
        primary: "hsl(var(--primary))",
        "primary-foreground": "hsl(var(--primary-foreground))",
        destructive: "hsl(var(--destructive))",
        "destructive-foreground": "hsl(var(--destructive-foreground))",
        accent: "hsl(var(--accent))",
      },
      borderRadius: {
        lg: "0.5rem",
        md: "0.375rem",
      },
    },
  },
  plugins: [],
};
