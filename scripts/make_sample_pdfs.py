"""Generate the sample PDF product manuals in data/docs/.

Run with: uv run python scripts/make_sample_pdfs.py
Each top-level list entry becomes one PDF page, so retrieval can cite page numbers.
"""

from pathlib import Path

from fpdf import FPDF

DOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "docs"

Page = tuple[str, list[str]]

AURORA_X2: list[Page] = [
    ("Aurora X2 Wireless Headphones - User Manual", [
        "Thank you for choosing the NovaTech Aurora X2. This manual covers setup, everyday "
        "use, battery care and troubleshooting.",
        "In the box: Aurora X2 headphones, USB-C charging cable (1 m), 3.5 mm audio cable, "
        "carry pouch and quick start guide. The hard Aurora X2 Carry Case is sold separately.",
        "Specifications: Bluetooth 5.3 with multipoint (two devices at once); codecs SBC, AAC "
        "and LDAC; 40 mm dynamic drivers; weight 250 g; charging via USB-C.",
    ]),
    ("Pairing and connections", [
        "First-time pairing: with the headphones off, press and hold the power button for 5 "
        "seconds until the LED flashes blue and white. Select 'Aurora X2' in your device's "
        "Bluetooth menu.",
        "Pairing a second device: open the NovaTech app and enable Multipoint, then repeat the "
        "pairing steps on the second device. Audio switches automatically to the device that "
        "starts playing.",
        "Wired mode: plug in the 3.5 mm cable. Wired mode works even when the battery is empty, "
        "but noise cancellation requires battery power.",
    ]),
    ("Noise cancellation and controls", [
        "Press the NC button on the left ear cup to cycle between Adaptive Noise Cancelling, "
        "Transparency and Off. Adaptive mode adjusts to your surroundings every few seconds.",
        "Touch controls on the right ear cup: double-tap to play/pause, swipe forward/back to "
        "skip tracks, swipe up/down to change volume, touch and hold for the voice assistant.",
        "Wear detection pauses playback when you take the headphones off. You can turn it off "
        "in the NovaTech app.",
    ]),
    ("Battery and charging", [
        "Battery life: up to 40 hours with noise cancellation on, 55 hours with it off. A "
        "10-minute quick charge gives about 5 hours of playback. A full charge takes 2 hours.",
        "Use any USB-C charger rated 5 V / 2 A or higher. The LED glows amber while charging "
        "and turns off when fully charged.",
        "For long-term storage, charge to about 50% every three months to protect the battery.",
    ]),
    ("Troubleshooting", [
        "Sound only in one ear cup: perform a factory reset (press and hold the power and "
        "volume-down buttons together for 10 seconds until the LED flashes red three times), "
        "then pair again. If the problem persists, the unit may be defective - contact support.",
        "Cannot pair: remove 'Aurora X2' from your device's Bluetooth list, reset the "
        "headphones and try again. Keep the headphones within 1 m during pairing.",
        "Firmware updates: install the NovaTech app, connect the headphones and open "
        "Settings > Firmware. Keep the headphones above 30% battery during updates.",
        "The headphones are not water resistant. Do not use them while swimming or showering.",
    ]),
]

PULSE_WATCH_S: list[Page] = [
    ("Pulse Watch S - Getting Started Guide", [
        "The Pulse Watch S is a smartwatch with health tracking, notifications and 5 ATM "
        "water resistance. It works with Android 10+ and iOS 15+ through the NovaTech Health app.",
        "Setup: charge the watch for 10 minutes, press and hold the side button to turn it on, "
        "then open the NovaTech Health app and tap 'Add device'. Keep the phone close to the watch.",
        "Specifications: 1.43-inch AMOLED display, 42 g with strap, GPS, heart-rate and SpO2 "
        "sensors, Bluetooth 5.2, 5 ATM water resistance.",
    ]),
    ("Battery and charging", [
        "Typical battery life is 7 days, or about 2 days with the always-on display enabled. "
        "Continuous GPS workouts last up to 18 hours.",
        "Charge with the magnetic charging puck. A full charge takes about 90 minutes. Make sure "
        "the charging contacts are clean and dry before charging.",
    ]),
    ("Water resistance", [
        "The Pulse Watch S is rated 5 ATM: suitable for swimming in a pool or shallow water. "
        "It is not suitable for diving, water skiing, saunas or hot showers.",
        "Rinse the watch with fresh water after swimming in the sea and dry it before charging. "
        "Water damage caused by misuse is not covered by the warranty.",
    ]),
    ("Health and fitness features", [
        "Heart rate is measured continuously; SpO2 can be measured on demand or overnight. "
        "Sleep tracking starts automatically when you wear the watch to bed.",
        "Over 100 workout modes are available. Auto-detection recognizes walking, running and "
        "cycling after about 10 minutes of activity.",
        "Health readings are for general wellness only and are not intended for medical diagnosis.",
    ]),
    ("Troubleshooting", [
        "Screen flickers or turns on randomly: update to the latest firmware in the NovaTech "
        "Health app (Device > Firmware). If flickering continues after charging, restart the "
        "watch by holding the side button for 15 seconds. Persistent flicker should be "
        "inspected through an in-store repair appointment.",
        "Watch not syncing: make sure Bluetooth is on and the NovaTech Health app is allowed "
        "to run in the background, then pull down to refresh in the app.",
        "Factory reset: on the watch, go to Settings > System > Reset. This erases all data "
        "stored on the watch.",
    ]),
]


class ManualPDF(FPDF):
    def __init__(self, title: str):
        super().__init__()
        self.doc_title = title
        self.set_auto_page_break(auto=True, margin=18)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", size=8)
        self.cell(0, 8, f"{self.doc_title} - page {self.page_no()}", align="C")


def write_pdf(filename: str, pages: list[Page]) -> Path:
    pdf = ManualPDF(pages[0][0])
    for heading, paragraphs in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", style="B", size=15)
        pdf.multi_cell(0, 9, heading)
        pdf.ln(3)
        pdf.set_font("Helvetica", size=11)
        for paragraph in paragraphs:
            pdf.multi_cell(0, 6, paragraph)
            pdf.ln(3)
    path = DOCS_DIR / filename
    pdf.output(str(path))
    return path


if __name__ == "__main__":
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for name, pages in [("aurora-x2-headphones-manual.pdf", AURORA_X2),
                        ("pulse-watch-s-guide.pdf", PULSE_WATCH_S)]:
        print("wrote", write_pdf(name, pages))
