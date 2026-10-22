import type { Metadata } from "next";
import "./globals.css";
import Sidebar from "@/components/Sidebar";
import { ToastProvider } from "@/components/Toast";

export const metadata: Metadata = {
  title: "Delegatron Dashboard",
  description: "Monitor and manage Delegatron pipeline runs",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="flex min-h-screen bg-gray-950 text-gray-100">
        <ToastProvider>
          <Sidebar />
          <main className="flex-1 p-8 overflow-auto">{children}</main>
        </ToastProvider>
      </body>
    </html>
  );
}
