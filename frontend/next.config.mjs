/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://localhost:8000/api/:path*" },
      { source: "/nuo/:path*", destination: "http://localhost:8000/nuo/:path*" },
      { source: "/ws", destination: "http://localhost:8000/ws" },
    ];
  },
};

export default nextConfig;
