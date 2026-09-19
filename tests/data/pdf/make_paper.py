"""Regenerate paper.pdf, the two-column IEEE-style fixture.

The PDF is committed so the test suite needs neither reportlab nor a network.
Run this only when the fixture itself should change:

    python tests/data/pdf/make_paper.py tests/data/pdf/paper.pdf

The reference list deliberately mixes the cases that matter: a DOI split across
a column break, an arXiv preprint, a retracted paper, a fabricated DOI, and a
reference with no identifier at all.
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer
from reportlab.lib.enums import TA_JUSTIFY
import sys

out = sys.argv[1]
doc = BaseDocTemplate(out, pagesize=letter,
                      leftMargin=0.6*inch, rightMargin=0.6*inch,
                      topMargin=0.7*inch, bottomMargin=0.7*inch)
w = (doc.width - 0.25*inch) / 2
frames = [Frame(doc.leftMargin, doc.bottomMargin, w, doc.height, id='l'),
          Frame(doc.leftMargin + w + 0.25*inch, doc.bottomMargin, w, doc.height, id='r')]
doc.addPageTemplates([PageTemplate(id='2col', frames=frames)])

title = ParagraphStyle('t', fontName='Helvetica-Bold', fontSize=14, leading=17, spaceAfter=10)
head  = ParagraphStyle('h', fontName='Helvetica-Bold', fontSize=9.5, leading=12,
                       spaceBefore=9, spaceAfter=4)
body  = ParagraphStyle('b', fontName='Times-Roman', fontSize=9, leading=11,
                       alignment=TA_JUSTIFY, spaceAfter=4)
ref   = ParagraphStyle('r', fontName='Times-Roman', fontSize=8, leading=9.6,
                       leftIndent=14, firstLineIndent=-14, spaceAfter=2.5)

story = [Paragraph("Towards Reliable Citation Verification in Machine Learning Papers", title)]
story.append(Paragraph("I. I<font size=7>NTRODUCTION</font>", head))
story.append(Paragraph(
    "Recent work on transformer architectures [1] has driven rapid progress across "
    "vision and language. Residual connections [2] remain foundational, and long-context "
    "variants [3] extend the approach to document-scale inputs. Some early claims in the "
    "clinical literature [4] have since been withdrawn, underscoring the need for "
    "verification. We build on the evaluation protocol of [5].", body))
story.append(Paragraph("II. M<font size=7>ETHOD</font>", head))
story.append(Paragraph(
    "Our method follows [1] and [2] closely, differing mainly in the normalization "
    "applied before attention. As in [3], we use a sliding window. We do not rely on [6], "
    "which was unavailable at the time of writing.", body))
story.append(Paragraph("III. R<font size=7>ESULTS</font>", head))
story.append(Paragraph(
    "Results are consistent with prior reports [1], [2]. We observe no regression on "
    "long inputs relative to [3].", body))
story.append(Spacer(1, 6))
story.append(Paragraph("R<font size=7>EFERENCES</font>", head))

refs = [
 '[1] A. Vaswani, N. Shazeer, N. Parmar, J. Uszkoreit, L. Jones, A. N. Gomez, '
 'L. Kaiser, and I. Polosukhin, "Attention is all you need," in <i>Advances in '
 'Neural Information Processing Systems</i>, vol. 30, 2017, pp. 5998-6008.',

 '[2] K. He, X. Zhang, S. Ren, and J. Sun, "Deep residual learning for image '
 'recognition," in <i>Proc. IEEE Conf. Comput. Vis. Pattern Recognit. (CVPR)</i>, '
 '2016, pp. 770-778. doi: 10.1109/CVPR.2016.90.',

 '[3] I. Beltagy, M. E. Peters, and A. Cohan, "Longformer: The long-document '
 'transformer," <i>arXiv preprint arXiv:2004.05150</i>, 2020.',

 '[4] A. J. Wakefield and S. H. Murch, "Ileal-lymphoid-nodular hyperplasia, '
 'non-specific colitis, and pervasive developmental disorder in children," '
 '<i>The Lancet</i>, vol. 351, no. 9103, pp. 637-641, 1998. '
 'doi: 10.1016/S0140-6736(97)11096-0.',

 '[5] J. Doe and R. Roe, "Quantum neural architectures for sentiment analysis," '
 '<i>Journal of Advanced Computing</i>, vol. 12, pp. 45-67, 2021. '
 'doi: 10.9999/jac.2021.99999.',

 '[6] M. Nobody, "A paper that exists only in a preprint server," 2019. '
 '[Online]. Available: https://example.org/nope',
]
for r in refs:
    story.append(Paragraph(r, ref))

doc.build(story)
print("wrote", out)
