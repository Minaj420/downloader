import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/app/App";

function stubHealthOk() {
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
}

describe("App shell", () => {
  it("renders primary navigation and the default page", async () => {
    stubHealthOk();
    render(<App />);

    expect(screen.getByRole("heading", { name: "New Download" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Queue" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Library" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
    expect(await screen.findByText("Backend online")).toBeInTheDocument();
  });

  it("marks the active navigation link with aria-current", async () => {
    stubHealthOk();
    render(<App />);

    expect(screen.getByRole("link", { name: "New Download" })).toHaveAttribute("aria-current", "page");

    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "New Download" })).not.toHaveAttribute("aria-current");
  });

  it("navigates between pages", async () => {
    stubHealthOk();
    render(<App />);

    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Queue" }));
    expect(screen.getByRole("heading", { name: "Queue" })).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Library" }));
    expect(screen.getByRole("heading", { name: "Library" })).toBeInTheDocument();
  });
});
