const apiOrigin = process.env.KUN_API_ORIGIN ?? "http://localhost:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiOrigin}/api/:path*` },
      { source: "/nuo/:path*", destination: `${apiOrigin}/nuo/:path*` },
      { source: "/ws", destination: `${apiOrigin}/ws` },
    ];
  },
};

export default nextConfig;
