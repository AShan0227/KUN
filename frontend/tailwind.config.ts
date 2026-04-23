import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        kun: {
          ink: "#1A1A1A",
          fog: "#F3F5F6",
          accent: "#2E5BFF",
          side: "#F7F3EC",   // 傩 side channel tone
          good: "#0E8A5F",
          warn: "#E3A92B",
          bad: "#D7423F",
        },
      },
    },
  },
  plugins: [],
};

export default config;
