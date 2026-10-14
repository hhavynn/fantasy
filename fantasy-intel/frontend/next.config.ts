import type { NextConfig } from "next";

// Local dashboard: every page reads fresh data from the FastAPI backend, so we
// use plain dynamic rendering rather than Cache Components.
const nextConfig: NextConfig = {};

export default nextConfig;
