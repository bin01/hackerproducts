import { defineConfig } from "astro/config";

export default defineConfig({
  build: { format: "directory" },
  devToolbar: { enabled: false },
});
