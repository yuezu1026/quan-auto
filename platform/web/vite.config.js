import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 构建产物直接落进 Java 侧的静态资源目录：
//   platform/web  --npm run build-->  platform/api/src/main/resources/static/
// 于是 `mvn spring-boot:run` 一个进程就能同时吐 API 和页面，不需要第二个服务、
// 也不需要配 CORS（同源）。这条路径是**跨模块的隐式契约**，改任一侧都要一起改。
export default defineConfig({
  plugins: [react()],
  // 相对路径：产物既能在 http://localhost:8080/ 下打开，也能从文件系统直接开。
  base: "./",
  build: {
    outDir: "../api/src/main/resources/static",
    // 必须清空，否则上一次构建留下的旧文件名会和新的一起躺在 static/ 里被一起服务。
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // 开发模式下把 /api 转给后端，前端代码里因此可以始终只写 "/api/..."。
    proxy: {
      "/api": "http://localhost:8080",
    },
  },
  // 页面渲染测试（vitest + jsdom）：把 App 真的挂到 DOM 上，喂一份服务端下发的 JSON。
  // 用例放在 platform/web/test/ 而**不是** src/ —— src/ 是**出厂源码**，
  // tools/verify_platform_web.py 会把它整棵树拿去扫「有没有手抄规格表」（PW-SPEC-COPY）
  // 与「页面读了哪些字段」，测试文件混进去只会让那两条判据的扫描面变得含混。
  test: {
    environment: "jsdom",
    include: ["test/**/*.test.{js,jsx}"],
  },
});
