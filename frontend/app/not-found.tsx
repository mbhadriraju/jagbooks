import Link from "next/link";

export default function NotFound() {
  return <main id="main" className="wrap"><h1 className="page-title">Page not found</h1><Link href="/">Back to home</Link></main>;
}
