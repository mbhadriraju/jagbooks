import { FinanceWorkspace } from "@/components/finance/workspace";

export default function Home() {
  return (
    <main id="main" className="wrap home">
      <section className="hero">
        <h1>JagBooks</h1>
        <p>One financial workbook for Blue Valley West Debate. Bring together sticky notes, district activity reports, and manual entries.</p>
      </section>
      <FinanceWorkspace preview />
    </main>
  );
}
