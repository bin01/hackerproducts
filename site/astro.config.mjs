import { defineConfig } from "astro/config";

export default defineConfig({
  site: "https://hackerproducts.pages.dev",
  build: { format: "directory" },
  devToolbar: { enabled: false },
});
