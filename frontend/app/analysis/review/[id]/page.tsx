import { Suspense } from "react";
import { SourceReview } from "@/components/finance/review";

type Props = { params: Promise<{ id: string }> };

export default function Review({ params }: Props) {
  return (
    <main id="main" className="wrap">
      <Suspense fallback={<p className="notice" role="status">Loading source review…</p>}>
        <ReviewContent params={params} />
      </Suspense>
    </main>
  );
}

async function ReviewContent({ params }: Props) {
  return <SourceReview id={(await params).id} />;
}
