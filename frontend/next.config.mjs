const apiOrigin = process.env.KUN_API_ORIGIN ?? "http://localhost:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiOrigin}/api/:path*` },
      { source: "/nuo/:path*", destination: `${apiOrigin}/nuo/:path*` },
      // Audit F153: cockpit/page.tsx fetches /cockpit/* (writes-status,
      // capabilities, supervisor, ensemble, missions). Without this rewrite those
      // hit the Next server (404) instead of being proxied to the API origin.
      { source: "/cockpit/:path*", destination: `${apiOrigin}/cockpit/:path*` },
      { source: "/ws", destination: `${apiOrigin}/ws` },
    ];
  },
};

export default nextConfig;
