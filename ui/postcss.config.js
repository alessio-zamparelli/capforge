// Tailwind v4 ships its PostCSS integration as a separate package; autoprefixer
// is built into the v4 pipeline, so the plugin list stays short.
export default {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};
