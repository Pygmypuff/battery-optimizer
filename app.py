import customtkinter as ctk

# ── Theme ────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("System")   # "Light" | "Dark" | "System"
ctk.set_default_color_theme("blue") # "blue" | "green" | "dark-blue"


# ── App class ────────────────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window setup
        self.title("My App")
        self.geometry("520x400")
        self.minsize(400, 300)

        # Allow the main column to stretch with the window
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)   # output box row

        # ── Widgets ───────────────────────────────────────────────────────────

        # Title label
        self.label_title = ctk.CTkLabel(
            self,
            text="My App",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        self.label_title.grid(row=0, column=0, padx=20, pady=(20, 4), sticky="w")

        # Subtitle / description
        self.label_sub = ctk.CTkLabel(
            self,
            text="Enter some input and press Run.",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        )
        self.label_sub.grid(row=1, column=0, padx=20, pady=(0, 12), sticky="w")

        # Input row (entry + button side-by-side)
        self.frame_input = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_input.grid(row=2, column=0, padx=20, pady=0, sticky="ew")
        self.frame_input.grid_columnconfigure(0, weight=1)

        self.entry = ctk.CTkEntry(
            self.frame_input,
            placeholder_text="Type something here…",
            height=38,
        )
        self.entry.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self.entry.bind("<Return>", lambda _e: self.on_run())

        self.btn_run = ctk.CTkButton(
            self.frame_input,
            text="Run",
            width=80,
            height=38,
            command=self.on_run,
        )
        self.btn_run.grid(row=0, column=1)

        # Output text box
        self.textbox = ctk.CTkTextbox(self, state="disabled", wrap="word")
        self.textbox.grid(row=3, column=0, padx=20, pady=(12, 8), sticky="nsew")

        # Status bar + clear button
        self.frame_bottom = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_bottom.grid(row=4, column=0, padx=20, pady=(0, 16), sticky="ew")
        self.frame_bottom.grid_columnconfigure(0, weight=1)

        self.label_status = ctk.CTkLabel(
            self.frame_bottom,
            text="Ready.",
            font=ctk.CTkFont(size=12),
            text_color="gray",
        )
        self.label_status.grid(row=0, column=0, sticky="w")

        self.btn_clear = ctk.CTkButton(
            self.frame_bottom,
            text="Clear",
            width=70,
            height=28,
            fg_color="transparent",
            border_width=1,
            command=self.on_clear,
        )
        self.btn_clear.grid(row=0, column=1)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _append_output(self, text: str) -> None:
        """Write text to the read-only output box."""
        self.textbox.configure(state="normal")
        self.textbox.insert("end", text + "\n")
        self.textbox.configure(state="disabled")
        self.textbox.see("end")

    def _set_status(self, msg: str) -> None:
        self.label_status.configure(text=msg)

    # ── Event handlers ────────────────────────────────────────────────────────

    def on_run(self) -> None:
        """Called when the user clicks Run (or presses Enter)."""
        user_input = self.entry.get().strip()
        if not user_input:
            self._set_status("⚠️  Please enter some input first.")
            return

        # ── Your logic goes here ──────────────────────────────────────────
        result = self.process(user_input)
        # ─────────────────────────────────────────────────────────────────

        self._append_output(f"> {user_input}\n{result}")
        self.entry.delete(0, "end")
        self._set_status("Done.")

    def on_clear(self) -> None:
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")
        self._set_status("Cleared.")

    # ── Business logic ────────────────────────────────────────────────────────

    def process(self, text: str) -> str:
        """
        Replace this with your actual logic.
        Receives the user's input string, returns a result string.
        """
        return f"You said: {text} (replace this with real logic)"


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()