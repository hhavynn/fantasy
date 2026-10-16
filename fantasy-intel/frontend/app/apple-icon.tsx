import { ImageResponse } from "next/og";

// iOS home-screen icon: the same "fi" mark as icon.svg on the Night background (iOS rounds the corners).
export const size = { width: 180, height: 180 };
export const contentType = "image/png";

export default function AppleIcon() {
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", background: "#0a0d12" }}>
        <svg width="180" height="180" viewBox="0 0 32 32">
          <g fill="none" stroke="#6aaeff" strokeWidth="3.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M11.4 24.6V13.4c0-3.4 2-5 5.6-4.7" />
            <path d="M8 14.2h8.2" />
            <path d="M22.4 14.2v10.4" />
          </g>
          <circle cx="22.4" cy="8.9" r="2.3" fill="#6aaeff" />
        </svg>
      </div>
    ),
    size,
  );
}
