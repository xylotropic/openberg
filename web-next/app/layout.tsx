import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import "./globals.css";

const bergoom = localFont({
  src: [
    { path: "../public/fonts/bergoom/Bergoom-Regular.woff2", weight: "400", style: "normal" },
    { path: "../public/fonts/bergoom/Bergoom-Semibold.woff2", weight: "500 600", style: "normal" },
    { path: "../public/fonts/bergoom/Bergoom-Bold.woff2", weight: "700 900", style: "normal" },
  ],
  variable: "--font-bergoom",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Calibrain Harness",
  description: "Calibrain Harness: forward-test trading on crypto perpetuals with calibrated classification models (Drex Finance) on Bybit testnet, and measure whether the model has an edge.",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#000000" };

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={bergoom.variable}>
      <body>{children}</body>
    </html>
  );
}
