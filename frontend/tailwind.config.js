/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Data Validator palette (sampled from the data_observability Figma).
        navy: { DEFAULT: "#182050", 600: "#232c63", 700: "#182050", 800: "#121a40", 900: "#0d1230" },
        teal: {
          DEFAULT: "#00a8c8",
          50: "#e8f8fc",
          100: "#c6edf5",
          200: "#9fe0ee",
          500: "#00a8c8",
          600: "#0092ae",
          700: "#007a94",
        },
        page: "#f6f8fa",
        ink: "#182050",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(16,24,40,0.04), 0 1px 3px rgba(16,24,40,0.06)",
      },
    },
  },
  plugins: [],
};
