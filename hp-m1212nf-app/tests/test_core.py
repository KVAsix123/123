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
