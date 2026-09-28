import { useState } from "react";
import { ApiRequestError } from "../api/client";
import { useAnalyzeUrl } from "../api/analysis";
import { AnalysisResult } from "../features/new-download/AnalysisResult";
import { AnalyzeForm } from "../features/new-download/AnalyzeForm";
import { AUTO_PLATFORM_VALUE } from "../features/new-download/platforms";

export function NewDownloadPage() {
  const [url, setUrl] = useState("");
  const [platform, setPlatform] = useState(AUTO_PLATFORM_VALUE);
  const analyze = useAnalyzeUrl();

  const platformHint = platform === AUTO_PLATFORM_VALUE ? null : platform;

  const runAnalysis = (nextUrl: string, hint: string | null) => {
    analyze.mutate({ url: nextUrl.trim(), platform_hint: hint });
  };

  const handleSubmit = () => {
    runAnalysis(url, platformHint);
  };

  const handleUseDetectedPlatform = (detected: string) => {
    setPlatform(detected);
    // Re-analyze the URL that produced this result, not whatever the input
    // currently contains; the user may have edited it after analyzing.
    runAnalysis(analyze.data?.original_url ?? url, detected);
  };

  const error = analyze.error;
  const retryable = error instanceof ApiRequestError && error.retryable;

  return (
    <section>
      <h1>New Download</h1>
      <AnalyzeForm
        url={url}
        platform={platform}
        isPending={analyze.isPending}
        onUrlChange={setUrl}
        onPlatformChange={setPlatform}
        onSubmit={handleSubmit}
      />

      {error && (
        <div className="error-banner" role="alert">
          <p>{error.message}</p>
          {retryable && (
            <button type="button" onClick={handleSubmit}>
              Retry
            </button>
          )}
        </div>
      )}

      {analyze.data && (
        <AnalysisResult
          result={analyze.data}
          onUseDetectedPlatform={handleUseDetectedPlatform}
        />
      )}
    </section>
  );
}
