# Newsletter images: recommended upload sizes

This report covers `template/brevo_template_1.html`, the version that is live in Brevo template 1 (sha256 starts `61327962`).

It was checked on 2026-10-13:
- **Image files:** downloaded from coinedition.com at 10:20 UTC and measured from their file headers.
- **Display sizes:** worked out from the template's own widths, padding and phone rules.

Nothing in the template, the newsletter content or Brevo was changed.

---

## 1. Recommended upload sizes

Upload images at these pixel sizes so they stay sharp and readable on both desktop and phones.

| Image (section) | Upload this size | Format | Shows on desktop | Shows on phones (360–430 px wide) | Template change needed? |
|---|---|---|---|---|---|
| **Coin Edition logo** (masthead and sign-off use the same file) | **720 × 178 px** | PNG, solid white background | 240 × 59 (masthead), 200 × 50 (sign-off) | the same: the logo doesn't shrink on phones | No |
| **Hero banner** (under the title) | **1280 × 256 px** (5 : 1) | JPG | 640 × 128 | 334 × 67 to 404 × 81 | No |
| **Partner Spotlight banner** | **1136 × 284 px** (4 : 1) | PNG, or high-quality JPG | 568 × 142 | 290 × 73 to 360 × 90 | **Yes:** `height="77"` becomes `height="142"` (one line) |
| **Story images 1 and 2** (switched off today) | **1056 × 594 px** (16 : 9) | JPG | 528 × 297 | 252 × 142 to 322 × 181 | Yes: they have to be switched on first |

### Why these sizes

- **At least twice the desktop display size.** Phones and many laptops have high-density screens, so a file needs at least 2 image pixels per screen pixel to look sharp. The same file serves phones, where it is shown even smaller and so has 3–4 pixels per screen pixel. The logo gets 3x (720 px for a 240 px slot) because it is small, has fine lettering and is a tiny file.
- **The ratios above.** The template states every image's width and height, and classic Outlook for Windows uses those numbers as they are. A file with a different ratio is stretched there unless the template's `height` is updated too. Gmail, Apple Mail and others resize correctly either way.
- **Why the partner banner should become 4 : 1.** At its current 7.4 : 1 it is only 39–49 px tall on phones, so its date and venue cannot be made readable at any resolution. At 4 : 1 it is 73–90 px tall on phones, enough for 2–3 lines of readable text.
  - Changing the shape needs `height="77"` changed to `height="142"` on line 484 of the template (not done; say if you want it).
  - If you'd rather leave the template alone, upload **1136 × 154 px** (the current 7.38 : 1). It will be sharp, but the text stays small on phones.
- **Hero option with more room for text.** If the hero must carry more text, use **1280 × 427 px (3 : 1)**: 640 × 213 on desktop, about 364 × 121 on a 390 px phone. This needs `height="128"` changed to `height="213"` on line 205.

### Design rules for clear text in the banners

1. **Smallest text at least 42 px tall in the uploaded file.** On a 390 px phone, the hero and partner banners are shown at about 57% of their desktop size. Text 42 px tall in a 2x file is 21 px on desktop and about 12 px on a phone, which is the smallest that reads comfortably. Today the hero's tagline is about 4 px on desktop and the partner banner's small print about 3–5 px on a phone.
2. **No QR codes in email images.** They are far too small to scan, and someone reading on a phone can't scan their own screen.
3. **JPG or PNG, never WebP.** Classic Outlook for Windows can't show WebP (see section 4).
4. **The logo on a solid white background, not transparent.** It is navy artwork. Some mail apps darken white backgrounds in dark mode, even though the template asks for light mode only, and a transparent navy logo then nearly disappears.
5. **Each file under about 100 KB if possible.** The current three files total 77 KB.
6. **A new file name for each new version.** coinedition.com serves these images with a 120-day cache (`Cache-Control: max-age=10368000`), so an overwritten file can keep showing the old version. Then update the URL in the template and paste the template into Brevo as usual.

---

## 2. The images the template uses today

The template shows **4 images from 3 files**, plus **2 story-image slots that are commented out**. There are no background images and no Outlook-only (VML) images. The 🔥 emoji and the social links are text.

| # | Image | Section (template line) | URL | Current file | Ratio | Weight | Template size (`width` × `height`) |
|---|---|---|---|---|---|---|---|
| 1 | Coin Edition logo | Masthead (line 161) | https://coinedition.com/wp-content/uploads/2022/07/CE-LOGO-WITH-TOG-02-min.png | PNG 2920 × 723, transparent | 4.04 : 1 | 26.0 KB | 240 × 59 |
| 2 | Hero banner | Hero banner, under the title (line 204) | https://coinedition.com/wp-content/uploads/2026/10/coinedition-cover-reddit01.webp | WebP 1920 × 384 | 5.00 : 1 | 38.7 KB | 640 × 128 |
| 3 | Unchained Summit India banner | Partner Spotlight (line 483) | https://coinedition.com/wp-content/uploads/2026/10/unchained-summit-india02.webp | WebP 827 × 112 | 7.38 : 1 | 12.3 KB | 568 × 77 |
| 4 | Coin Edition logo (same file as 1) | Sign-off, "Stay Ahead of the Market" (line 836) | same as 1 | PNG 2920 × 723 | 4.04 : 1 | – | 200 × 50 |
| 5–6 | Story image 1 / 2 (inactive) | Top Stories (lines 407 and 452, inside comments) | placeholder `YOUR_STORY_N_IMAGE_URL` | none | not set | – | 528 × (no height) |

The template's `width`/`height` values match the files' ratios to within 1%, so nothing is distorted today.

---

## 3. Image by image

### Image 1: Coin Edition logo (masthead)

- **Template:** `width="240" height="59"`; CSS `width:240px; max-width:100%; height:auto`.
  - Links to https://coinedition.com/. Alt text "Coin Edition".
- **Current file:** 2920 × 723 PNG, 12 times wider than it is shown. It is still a light file (26 KB), but that much shrinking is unnecessary and can soften fine edges in some clients.
- **Shown at:** 240 × 59 on desktop and phones. The logo has a fixed width and fits every phone from 310 px up.
- **Responsive issues:** the transparent background with navy artwork is a dark-mode risk; the template's own note says it "needs a white background". The 59 px height against an exact 59.4 px has no visible effect.
- **Upload:** 720 × 178 px PNG on white (minimum 480 × 119).

### Image 2: Hero banner (under the title)

- **Template:** `width="640" height="128"`; CSS `width:100%; height:auto`.
  - Not linked. Alt text "Coin Edition – Your Global News Destination", which shows white on navy when images are off.
- **Current file:** 1920 × 384 WebP, 3x the desktop size, so it is sharp enough. The file name ("cover-reddit") suggests a social-media cover reused in the email.
- **Shown at:** 640 × 128 on desktop; 334 × 67 to 404 × 81 on phones.
- **Responsive issues** (sizes measured by eye from the file):
  - WebP is not shown in classic Outlook for Windows.
  - The logo, tagline and two "Follow Us" QR codes sit in the middle of a very wide picture, so they end up small. On desktop the tagline is about 4 px tall, the "Follow Us" labels about 3 px and the QR codes about 23 px square. On a 390 px phone each is 57% of that, for example QR codes of about 13 px.
  - The QR codes can't be used: they can't be scanned at that size, and the banner isn't a link either.
- **Upload:** 1280 × 256 px JPG with only large elements (rules above). Alternatively 1280 × 427 px (3 : 1) with the one-line template change.

### Image 3: Unchained Summit India banner (Partner Spotlight)

- **Template:** `width="568" height="77"`; CSS `width:100%; height:auto`.
  - Links to https://unchainedsummit.com/india/. Alt text "Unchained Summit India · 05–06 Nov, 2026 · Courtyard by Marriott, Mumbai".
- **Current file:** 827 × 112 WebP. That is only 1.46x the desktop size, so it looks slightly soft on high-density laptop screens. On phones it is about 2.6x, which is fine.
- **Shown at:** 568 × 77 on desktop; 290 × 39 to 360 × 49 on phones.
- **Responsive issues:**
  - WebP is not shown in classic Outlook for Windows.
  - The very wide shape makes it only 43 px tall on a 390 px phone. Measured by eye, the date and venue lines are about 12 px tall on desktop but about 7 px on that phone. The web address is about 5 px and "An event by" about 3–4 px, which can't be read.
  - The creative itself reads "Marriott,Mumbai", with no space after the comma. The alt text has it right.
- **Upload:** 1136 × 284 px (4 : 1) with the one-line template change, or 1136 × 154 px with none.

### Image 4: Coin Edition logo (sign-off, "Stay Ahead of the Market")

- **Template:** `width="200" height="50"`; CSS `width:200px; max-width:100%; height:auto`.
  - Links to https://coinedition.com/. Alt text "CoinEdition.com".
- **Shown at:** 200 × 50 on desktop and phones. It sits on a white section directly above the navy footer.
- **Responsive issues:** the same as image 1, with dark mode the main risk.
- **Upload:** nothing separate. It uses the same 720 × 178 px file as the masthead (3.6x here).

### Images 5–6: Story images 1 and 2 (Top Stories, inactive)

- **Template:** inside HTML comments, so they are neither shown nor sent. Story 3 has no slot.
  - The planned tag is `width="528"`, CSS `width:100%; max-width:528px; height:auto`, with no `height` attribute and empty alt text.
  - Its `src` is the placeholder `YOUR_STORY_N_IMAGE_URL`. The project's template check would refuse it if the slot were switched on as it is.
- **Would show at:** 528 wide on desktop; 252–322 wide on phones, because the story number badge takes 38 px.
- **Before switching them on** (a template edit): add `height="297"` for a 16 : 9 image, because classic Outlook ignores `height:auto`. Also add real alt text and use a JPG URL.
- **Upload:** 1056 × 594 px JPG.

---

## 4. Responsive issues that affect every image

- **WebP and Outlook.** caniemail's tests (last run 2021-02-06) show these clients don't display WebP: classic Outlook for Windows 2007–2019, Outlook for Mac 2016, Apple Mail 13 and iOS 13. Gmail displays WebP by converting it to JPG first, which is harmless here because neither banner has transparency.
  - coinedition.com sends the same WebP file even to clients that say they can't take WebP (checked 2026-10-13), so nothing falls back automatically.
  - The fix is JPG/PNG uploads and new `src` URLs in the template.
- **Apps that ignore the phone layout.** The phone sizes in this report come from the template's `@media` rules (screens up to 620 px). caniemail's data (2023-12-13) shows these rules are ignored by:
  - the Gmail apps, for non-Google accounts (for example an Outlook.com address added to the Gmail app)
  - Gmail in a phone browser

  There the email keeps its 640 px desktop layout, and the app shrinks the whole email or lets the reader scroll sideways. That is another reason to keep text in images large.
- **Screens 621 to about 665 px wide.** The phone layout starts at 620 px, but the desktop card needs about 664 px (640 + 12 px margin on each side). In that narrow range, which covers some foldables and small tablets, the email scrolls slightly sideways.
- **Images turned off.** Classic Outlook and some company mail systems block images until the reader allows them. All four live images have meaningful, styled alt text, so the email still makes sense.
- **Already handled well:**
  - every live image has `width` and `height` attributes, which Outlook needs
  - `display:block`, so there are no gaps under images
  - fluid `width:100%` / `max-width:100%` sizing
  - Outlook's PNG setting and 96-DPI setting
  - bicubic resizing (`-ms-interpolation-mode`)

When Brevo sends the email it may add its own invisible 1 × 1 open-tracking image. It isn't part of the template and needs no size.

---

## 5. How the display sizes were worked out

- **Desktop:** the email card is 640 px wide. Content sections have 36 px padding on each side, which leaves 568 px. The hero runs the full card width.
- **Phones (screens up to 620 px wide):** the card fills the screen minus a 12 px page margin on each side and its 1 px border. Content sections have 22 px padding on each side. The logos keep their fixed 240 px / 200 px widths.

| Screen width | Card | Hero (5 : 1) | Partner banner, current 7.38 : 1 | Partner banner, recommended 4 : 1 | Story image (16 : 9) |
|---|---|---|---|---|---|
| 360 px | 334 | 334 × 67 | 290 × 39 | 290 × 73 | 252 × 142 |
| 375 px | 349 | 349 × 70 | 305 × 41 | 305 × 76 | 267 × 150 |
| 390 px | 364 | 364 × 73 | 320 × 43 | 320 × 80 | 282 × 159 |
| 414 px | 388 | 388 × 78 | 344 × 47 | 344 × 86 | 306 × 172 |
| 430 px | 404 | 404 × 81 | 360 × 49 | 360 × 90 | 322 × 181 |

---

## 6. Sources

- **Template markup:** `template/brevo_template_1.html`. Lines 106–120 hold the phone rules and line 142 the 640 px card. The images are on lines 161, 204, 407, 452, 483 and 836.
- **Image files:** downloaded 2026-10-13 10:20 UTC. Pixel sizes were read from the PNG/WebP file headers; weights are as served, and the cache header was observed then.
- **caniemail:** [WebP images](https://www.caniemail.com/features/image-webp/) and [media queries](https://www.caniemail.com/features/css-at-media/), from the raw data in the caniemail GitHub repository.
