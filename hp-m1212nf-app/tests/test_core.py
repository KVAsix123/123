"""Тесты логики, не требующей реального МФУ: python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageDraw  # noqa: E402

import image_tools as it  # noqa: E402
import print_backend as pb  # noqa: E402


def fake_scan(dpi=150, mode="L"):
    """Лист A4 с серым «бумажным» фоном, шумом и строками текста."""
    w, h = int(8.27 * dpi), int(11.69 * dpi)
    img = Image.new("RGB", (w, h), (222, 218, 210))
    d = ImageDraw.Draw(img)
    for y in range(dpi, h - dpi, dpi // 5):
        d.text((dpi, y), "Lorem ipsum dolor sit amet, consectetur adipiscing elit", fill=(35, 35, 40))
    # Шум матрицы сканера (σ ≈ 5) — именно он раздувает JPEG без очистки фона.
    noise = Image.effect_noise((w, h), 25).convert("RGB")
    return Image.blend(img, noise, 0.2).convert(mode)


class ImageToolsTest(unittest.TestCase):
    def test_clean_background_makes_paper_white(self):
        out = it.clean_background(fake_scan())
        self.assertGreater(out.histogram()[255] / (out.width * out.height), 0.8)

    def test_bw_is_one_bit_and_tiny(self):
        out = it.process(fake_scan(), "bw", True)
        self.assertEqual(out.mode, "1")
        data, ext = it.encode_page(out, 0, 150)
        self.assertEqual(ext, ".png")
        self.assertLess(len(data), 60_000)

    def test_clean_background_shrinks_jpeg(self):
        raw = fake_scan()
        plain, _ = it.encode_page(it.process(raw, "gray", False), 75, 150)
        clean, _ = it.encode_page(it.process(raw, "gray", True), 75, 150)
        self.assertLess(len(clean), len(plain) / 2)

    def test_pdf_page_size_follows_dpi(self):
        import pymupdf

        a = Image.new("L", (int(8.27 * 300), int(11.69 * 300)), 255)
        b = Image.new("L", (int(8.27 * 150), int(11.69 * 150)), 255)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.pdf")
            it.save_pages([a, b], path, 75, [300, 150])
            with pymupdf.open(path) as doc:
                self.assertEqual(doc.page_count, 2)
                for page in doc:  # обе страницы — A4 (595×842 pt), несмотря на разный dpi
                    self.assertAlmostEqual(page.rect.width, 595, delta=2)
                    self.assertAlmostEqual(page.rect.height, 842, delta=2)

    def test_formats(self):
        pages = [it.process(fake_scan(100), "bw", True), it.process(fake_scan(100), "gray", True)]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(len(it.save_pages(pages, os.path.join(tmp, "a.jpg"), 80, 100)), 2)
            tif = it.save_pages(pages, os.path.join(tmp, "a.tif"), 80, 100)[0]
            with Image.open(tif) as im:
                self.assertEqual(im.n_frames, 2)


def fake_passport_on_bed(dpi=150):
    """Бледный разворот паспорта 176×125 мм, прижатый к углу стекла A4."""
    bed = Image.new("RGB", (int(8.27 * dpi), int(11.69 * dpi)), (247, 247, 245))
    pw, ph = int(176 / 25.4 * dpi), int(125 / 25.4 * dpi)
    doc = Image.new("RGB", (pw, ph), (228, 233, 218))  # пастельный фон, почти как крышка
    ImageDraw.Draw(doc).text((pw // 2, ph // 2), "MVD", fill=(60, 60, 140))
    bed.paste(doc, (0, 0))
    noise = Image.effect_noise(bed.size, 25).convert("RGB")
    return Image.blend(bed, noise, 0.06), (pw, ph)


class LayoutTest(unittest.TestCase):
    def test_autocrop_finds_pale_document_in_corner(self):
        bed, (pw, ph) = fake_passport_on_bed()
        out = it.autocrop_to_content(bed)
        self.assertAlmostEqual(out.width, pw, delta=pw * 0.03)
        self.assertAlmostEqual(out.height, ph, delta=ph * 0.03)

    def test_autocrop_keeps_blank_page(self):
        blank = Image.new("RGB", (600, 800), (247, 247, 245))
        self.assertEqual(it.autocrop_to_content(blank).size, blank.size)

    def test_compose_on_a4_keeps_real_size(self):
        dpi = 150
        spread = Image.new("RGB", (int(176 / 25.4 * dpi), int(125 / 25.4 * dpi)), "pink")
        sheet, out_dpi = it.compose_on_a4([(spread, dpi), (spread.copy(), dpi)])
        self.assertEqual(out_dpi, dpi)
        self.assertEqual(sheet.size, (int(8.27 * dpi), int(11.69 * dpi)))
        # Ширина розовой области на листе — те же 176 мм, что у оригинала.
        row = [sheet.getpixel((x, int(sheet.height * 0.25))) for x in range(sheet.width)]
        pink = sum(1 for px in row if px != (255, 255, 255))
        self.assertAlmostEqual(pink / dpi * 25.4, 176, delta=2)

    def test_as_saved_matches_encoded_size(self):
        img = it.process(fake_scan(), "gray", True)
        saved, size = it.as_saved(img, 60, 150)
        self.assertEqual(saved.size, img.size)
        self.assertEqual(size, len(it.encode_page(img, 60, 150)[0]))


class PrintPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import pymupdf

        self.pdf = os.path.join(self.tmp.name, "doc.pdf")
        doc = pymupdf.open()
        for _ in range(5):
            doc.new_page()
        doc.save(self.pdf)
        doc.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_page_range(self):
        self.assertEqual(pb.parse_page_range("1-3, 5", 10), [0, 1, 2, 4])
        self.assertEqual(pb.parse_page_range("8-", 10), [7, 8, 9])
        self.assertEqual(pb.parse_page_range("", 3), [0, 1, 2])
        with self.assertRaises(pb.PrintError):
            pb.parse_page_range("abc", 3)

    def test_simple_copies_are_collated(self):
        plan = pb.build_plan([self.pdf], pb.PrintOptions(copies=2, page_range="1-2"))
        self.assertEqual([r.index for r in plan.passes[0]], [0, 1, 0, 1])

    def test_manual_duplex_odd_pages(self):
        plan = pb.build_plan([self.pdf], pb.PrintOptions(manual_duplex=True))
        front, back = plan.passes
        self.assertEqual([r.index for r in front], [0, 2, 4])
        # Оборот в обратном порядке; для 5-й страницы — пустая оборотная сторона.
        self.assertEqual([r.kind for r in back], ["blank", "pdf", "pdf"])
        self.assertEqual([r.index for r in back[1:]], [3, 1])

    def test_manual_duplex_each_copy_starts_new_sheet(self):
        plan = pb.build_plan([self.pdf], pb.PrintOptions(copies=2, page_range="1-3", manual_duplex=True))
        front, back = plan.passes
        self.assertEqual(len(front), 4)  # 2 копии × 2 листа
        self.assertEqual(len(front), len(back))

    def test_other_files_go_to_associated_app(self):
        docx = os.path.join(self.tmp.name, "letter.docx")
        open(docx, "wb").close()
        plan = pb.build_plan([docx], pb.PrintOptions())
        self.assertEqual(plan.other_files, [docx])
        self.assertEqual(plan.passes, [])


if __name__ == "__main__":
    unittest.main()
