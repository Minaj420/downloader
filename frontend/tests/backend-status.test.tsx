import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { BackendStatus } from "../src/components/BackendStatus";

function renderStatus() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <BackendStatus />
    </QueryClientProvider>,
  );
}

describe("BackendStatus", () => {
  it("shows online when the health endpoint succeeds", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ status: "ok", database: "ok" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );

    renderStatus();

    expect(await screen.findByText("Backend online")).toBeInTheDocument();
  });

  it("shows offline when the health endpoint fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("unavailable", { status: 503 })),
    );

    renderStatus();

    expect(await screen.findByText("Backend offline")).toBeInTheDocument();
  });
});
