import type { Metadata } from "next";
import { IBM_Plex_Sans, JetBrains_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const jetbrainsMono = JetBrains_Mono({
  variable: "--font-jetbrains-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "DevAssist",
  description: "Ask questions about your codebase and get automatic pull request reviews.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className={`${plexSans.variable} ${jetbrainsMono.variable} font-sans antialiased`}>
        <header className="border-b border-line">
          <nav className="mx-auto flex max-w-5xl items-center justify-between px-4 py-4">
            <Link href="/" className="text-lg font-bold tracking-tight">
              Dev<span className="text-accent">Assist</span>
            </Link>
            <Link href="/dashboard" className="text-sm text-muted hover:text-accent">
              Dashboard
            </Link>
          </nav>
        </header>
        <main className="mx-auto max-w-5xl px-4 py-12">{children}</main>
      </body>
    </html>
  );
}
