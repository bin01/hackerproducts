import { defineConfig } from "astro/config";

export default defineConfig({
  site: "http://localhost:4321",
  build: { format: "directory" },
  devToolbar: { enabled: false },
});
