from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import Flowable

WIDTH, HEIGHT = A4

class MonoBlock(Flowable):
    def __init__(self, lines, font_size=7.5, leading=10):
        Flowable.__init__(self)
        self.lines = lines
        self.font_size = font_size
        self.leading = leading
        self.width = WIDTH - 4*cm
        self.height = len(lines) * leading + 4

    def draw(self):
        self.canv.setFont("Courier", self.font_size)
        y = self.height - self.leading
        for line in self.lines:
            self.canv.drawString(0, y, line)
            y -= self.leading

def make_styles():
    base = getSampleStyleSheet()
    cover_title = ParagraphStyle('CoverTitle', parent=base['Title'],
        fontSize=28, textColor=colors.HexColor('#1a237e'),
        spaceAfter=10, alignment=TA_CENTER, fontName='Helvetica-Bold')
    cover_sub = ParagraphStyle('CoverSub', parent=base['Normal'],
        fontSize=14, textColor=colors.HexColor('#283593'),
        spaceAfter=6, alignment=TA_CENTER, fontName='Helvetica')
    mod_heading = ParagraphStyle('ModHeading',
        fontSize=20, textColor=colors.white,
        fontName='Helvetica-Bold', alignment=TA_CENTER,
        spaceAfter=0, spaceBefore=0, leading=26)
    section_heading = ParagraphStyle('SectionHeading',
        fontSize=13, textColor=colors.HexColor('#1a237e'),
        fontName='Helvetica-Bold', spaceBefore=14, spaceAfter=6)
    q_style = ParagraphStyle('QStyle',
        fontSize=10.5, textColor=colors.HexColor('#b71c1c'),
        fontName='Helvetica-Bold', spaceBefore=10, spaceAfter=4,
        leading=14)
    ans_heading = ParagraphStyle('AnsHeading',
        fontSize=10, textColor=colors.HexColor('#1b5e20'),
        fontName='Helvetica-Bold', spaceBefore=2, spaceAfter=2)
    body = ParagraphStyle('Body', parent=base['Normal'],
        fontSize=9.5, leading=14, spaceAfter=4,
        alignment=TA_JUSTIFY, fontName='Helvetica')
    bullet = ParagraphStyle('Bullet', parent=base['Normal'],
        fontSize=9.5, leading=13, leftIndent=16, spaceAfter=2,
        bulletIndent=4, fontName='Helvetica')
    bold_body = ParagraphStyle('BoldBody', parent=base['Normal'],
        fontSize=9.5, leading=13, fontName='Helvetica-Bold', spaceAfter=2)
    formula = ParagraphStyle('Formula',
        fontSize=9.5, leading=13, fontName='Courier',
        leftIndent=20, spaceAfter=3, textColor=colors.HexColor('#1a237e'))
    note = ParagraphStyle('Note',
        fontSize=9, leading=12, fontName='Helvetica-Oblique',
        textColor=colors.HexColor('#4a148c'), leftIndent=10,
        spaceAfter=4, spaceBefore=3)
    return dict(cover_title=cover_title, cover_sub=cover_sub,
                mod_heading=mod_heading, section_heading=section_heading,
                q_style=q_style, ans_heading=ans_heading,
                body=body, bullet=bullet, bold_body=bold_body,
                formula=formula, note=note, base=base)

S = make_styles()

def HR():
    return HRFlowable(width="100%", thickness=1,
                      color=colors.HexColor('#90caf9'), spaceAfter=4, spaceBefore=4)

def Q(text):
    return Paragraph(f"Q: {text}", S['q_style'])

def A():
    return Paragraph("<b>Answer:</b>", S['ans_heading'])

def P(text):
    return Paragraph(text, S['body'])

def B(text):
    return Paragraph(f"• {text}", S['bullet'])

def Bold(text):
    return Paragraph(text, S['bold_body'])

def F(text):
    return Paragraph(text, S['formula'])

def Note(text):
    return Paragraph(f"<i>{text}</i>", S['note'])

def SH(text):
    return Paragraph(text, S['section_heading'])

def section_banner(title):
    data = [[Paragraph(title, S['mod_heading'])]]
    t = Table(data, colWidths=[WIDTH - 4*cm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#1a237e')),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
    ]))
    return t

def make_table(headers, rows, col_widths=None):
    data = [headers] + rows
    col_widths = col_widths or [(WIDTH - 4*cm)/len(headers)] * len(headers)
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1a237e')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#e8eaf6'), colors.white]),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#9fa8da')),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
    ]))
    return t

story = []

# ── COVER PAGE ────────────────────────────────────────────────────────────────
story.append(Spacer(1, 2*cm))
story.append(Paragraph("MODULE 4", S['cover_title']))
story.append(Spacer(1, 0.3*cm))
story.append(Paragraph("Energy Storage for EV and HEV", S['cover_sub']))
story.append(Spacer(1, 0.5*cm))
story.append(HRFlowable(width="60%", thickness=3, color=colors.HexColor('#1a237e'), hAlign='CENTER'))
story.append(Spacer(1, 0.5*cm))
story.append(Paragraph("Comprehensive Exam-Ready Notes",
    ParagraphStyle('ct2', fontSize=13, fontName='Helvetica-Bold',
    textColor=colors.HexColor('#283593'), alignment=TA_CENTER)))
story.append(Spacer(1, 0.4*cm))
story.append(Paragraph("All 2-Mark (Q1–Q40) & 8/10-Mark (Q1–Q21) Questions with Detailed Answers",
    ParagraphStyle('ct3', fontSize=10, fontName='Helvetica',
    textColor=colors.HexColor('#455a64'), alignment=TA_CENTER)))
story.append(Spacer(1, 1.5*cm))

topics = [
    [Paragraph("<b>Topic</b>", S['body']), Paragraph("<b>Coverage</b>", S['body'])],
    [P("Energy Storage Requirements"), P("Batteries, Fuel Cells, Flywheels, Specific Energy")],
    [P("Battery Cell Construction"), P("Electrodes, Electrolyte, Separator, Primary vs Secondary")],
    [P("Battery Parameters"), P("Capacity, Discharge Rate, SoC, SoD, DoD — Formulas")],
    [P("Types of Batteries"), P("Lead-Acid, NiCd, NiMH, Li-ion, Li-poly, Zn-Air, NaS, ZEBRA")],
    [P("Battery Modelling"), P("Peukert's Equation, Fractional Depletion Model (FDM)")],
    [P("Fuel Cells"), P("Basic Structure, Reactions, Characteristics, Battery vs Fuel Cell")],
    [P("Types of Fuel Cells"), P("AFC, PEMFC, DMFC, PAFC, MCFC, SOFC — Comparison")],
    [P("PEMFC"), P("Construction, Operation, Water Management, Poisoning Issues")],
    [P("Supercapacitors"), P("Working Principle, Comparison with Batteries, EV Applications")],
]
t = Table(topics, colWidths=[7*cm, 10.5*cm])
t.setStyle(TableStyle([
    ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1a237e')),
    ('TEXTCOLOR', (0,0), (-1,0), colors.white),
    ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#e8eaf6'), colors.white]),
    ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#9fa8da')),
    ('FONTSIZE', (0,0), (-1,-1), 9),
    ('TOPPADDING', (0,0), (-1,-1), 5),
    ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ('LEFTPADDING', (0,0), (-1,-1), 6),
]))
story.append(t)
story.append(PageBreak())

# ═══════════════════════════════════════════════════════════════════════════
# PART A — 2 MARK QUESTIONS
# ═══════════════════════════════════════════════════════════════════════════
story.append(section_banner("MODULE 4 — PART A: 2-MARK QUESTIONS"))
story.append(Spacer(1, 0.3*cm))

# ── SECTION 1: Energy Storage Requirements ────────────────────────────────
story.append(SH("1. ENERGY STORAGE REQUIREMENTS"))
story.append(HR())

story.append(Q("1. What is the basic energy requirement for electric vehicles?"))
story.append(A())
story.append(P("A basic requirement for electric vehicles (EVs) is a <b>portable source of electrical energy</b>, which is converted into mechanical energy in the electric motor for vehicle propulsion. Electrical energy is typically obtained through conversion of chemical energy stored in batteries and fuel cells. A flywheel is an alternative portable source where energy is stored in mechanical form."))
story.append(B("The energy source must be portable, rechargeable, high specific energy, high specific power"))
story.append(B("The biggest obstacle in commercializing EVs is finding a suitable high-energy-density portable energy source"))
story.append(Note("Exam Tip: Key phrase — 'portable source of electrical energy converted to mechanical energy in the motor.'"))
story.append(Spacer(1,0.15*cm))

story.append(Q("2. What is meant by operating life of a battery?"))
story.append(A())
story.append(P("<b>Operating life of a battery</b> is defined as the number of deep discharge cycles obtainable during its lifetime, or the number of service years expected in a certain application."))
story.append(B("Deep discharge cycle: discharging at least 80% of rated capacity"))
story.append(B("Calendar life: years of service"))
story.append(B("Cycle life: number of charge/discharge cycles before capacity falls below acceptable level"))
story.append(Note("Exam Tip: Distinguish between 'calendar life' (time-based) and 'cycle life' (use-based)."))
story.append(Spacer(1,0.15*cm))

story.append(Q("3. Why are batteries commonly used as energy sources in EVs?"))
story.append(A())
story.append(P("Batteries have been the most popular choice of energy source for EVs since the beginning of EV research because:")  )
story.append(B("They convert stored chemical energy directly into electrical energy on demand"))
story.append(B("They are portable, rechargeable, and scalable to required voltage and energy levels"))
story.append(B("They offer high specific power and high specific energy compared to other portable sources"))
story.append(B("They support regenerative braking — recovering energy during braking"))
story.append(B("Commercially available EVs and HEVs use batteries as the primary electrical energy source"))
story.append(Note("Exam Tip: Mention 'rechargeable', 'regenerative braking support', and 'scalable voltage/energy' as key reasons."))
story.append(Spacer(1,0.15*cm))

story.append(Q("4. What is meant by battery module?"))
story.append(A())
story.append(P("A <b>battery module</b> is formed when one or more electrolytic cells are connected in series and the grouped cells are enclosed in a casing (housing). A battery module is an intermediate structural unit between individual battery cells and the complete battery pack."))
story.append(F("Cell → (series connection) → Battery Module → (series-parallel) → Battery Pack"))
story.append(Note("Exam Tip: Cell → Module → Pack is the hierarchy. Module = cells + casing."))
story.append(Spacer(1,0.15*cm))

story.append(Q("5. Define battery pack."))
story.append(A())
story.append(P("A <b>battery pack</b> is a collection of individual battery modules connected in a series and parallel combination to deliver the desired voltage and energy to the power electronic drive system of an EV or HEV."))
story.append(B("Series connection: increases total voltage"))
story.append(B("Parallel connection: increases total capacity (Ah)"))
story.append(B("The pack must deliver the voltage and energy required by the motor drive"))
story.append(Note("Exam Tip: Battery pack = modules in series-parallel. It is the complete energy storage unit installed in the vehicle."))
story.append(Spacer(1,0.15*cm))

story.append(Q("6. What are primary batteries?"))
story.append(A())
story.append(P("<b>Primary batteries</b> are batteries that <b>cannot be recharged</b> and are designed for a single discharge only. Once the stored chemical energy is completely discharged, the battery is discarded."))
story.append(B("Examples: Lithium batteries (watches, calculators, cameras); Manganese dioxide batteries (toys, radios, torches)"))
story.append(B("Chemical reaction during discharge is irreversible"))
story.append(B("NOT suitable for EVs and HEVs"))
story.append(Note("Exam Tip: Primary = single use, not rechargeable. Secondary = rechargeable."))
story.append(Spacer(1,0.15*cm))

story.append(Q("7. What are secondary batteries?"))
story.append(A())
story.append(P("<b>Secondary batteries</b> are batteries that <b>can be recharged</b> by passing an electric current in the direction opposite to the discharge current. During charging, electrical energy is converted back into chemical energy (reverse of discharge reaction)."))
story.append(B("Chemical reaction during charge is the reverse of the discharge reaction"))
story.append(B("Can be cycled repeatedly — charge and discharge many times"))
story.append(B("Examples: Lead-acid, NiCd, NiMH, Li-ion, Li-polymer batteries"))
story.append(Note("Exam Tip: All EV/HEV batteries are secondary batteries. The key word is 'rechargeable.'"))
story.append(Spacer(1,0.15*cm))

story.append(Q("8. Why are secondary batteries used in EVs and HEVs?"))
story.append(A())
story.append(P("Secondary batteries are used in EVs and HEVs because:"))
story.append(B("They are <b>rechargeable</b> — can be recharged during regenerative braking cycles or from external chargers"))
story.append(B("They support <b>regenerative braking</b> — energy recovered during braking is stored back in the battery"))
story.append(B("They can be <b>cycled repeatedly</b>, making them economically viable for long-term vehicle use"))
story.append(B("Recharging is possible when the vehicle is stopped using an external charger"))
story.append(Note("Exam Tip: The defining reason is 'rechargeable during regeneration cycles AND from external charger.' Primary batteries are not suitable."))
story.append(Spacer(1,0.15*cm))

# ── SECTION 2: Battery Parameters ────────────────────────────────────────────
story.append(SH("2. BATTERY PARAMETERS"))
story.append(HR())

story.append(Q("9. Define battery capacity."))
story.append(A())
story.append(P("<b>Battery capacity</b> is defined as the amount of free charge generated by the active material at the negative electrode and consumed by the positive electrode during complete discharge."))
story.append(F("Q_T = x·n·F  [Coulombs]"))
story.append(B("x = moles of limiting reactant = m_R / M_m"))
story.append(B("n = electrons produced per mole at negative electrode"))
story.append(B("F = Faraday's constant = 96,485 C/mol"))
story.append(P("In Ah: Q_T = 0.278·F·(m_R·n/M_m) Ah"))
story.append(Note("Exam Tip: Capacity = total charge deliverable. Unit: Ah (ampere-hour). 1 Ah = 3600 C."))
story.append(Spacer(1,0.15*cm))

story.append(Q("10. In what unit is battery capacity measured?"))
story.append(A())
story.append(P("Battery capacity is measured in <b>Ampere-hours (Ah)</b>."))
story.append(F("1 Ah = 3600 Coulombs (C)")  )
story.append(F("1 C = charge transferred in 1 second by 1 Ampere current"))
story.append(B("Small batteries: mAh (milliampere-hours)"))
story.append(B("EV battery packs: kWh (kilowatt-hours) for energy capacity"))
story.append(Note("Exam Tip: Capacity in Ah measures charge. Energy = Capacity × Voltage, measured in Wh or kWh."))
story.append(Spacer(1,0.15*cm))

story.append(Q("11. Define battery discharge rate."))
story.append(A())
story.append(P("The <b>discharge rate</b> is the current at which a battery is discharged. It is expressed as Q/h rate, where Q is the rated battery capacity and h is the discharge time in hours."))
story.append(F("Discharge rate = Q_T / t  [Amperes]"))
story.append(P("Example: For a 100 Ah battery:"))
story.append(F("Q/5 rate = 100 Ah / 5 h = 20 A"))
story.append(F("2Q rate = 100 Ah / 0.5 h = 200 A"))
story.append(Note("Exam Tip: Higher discharge rate (shorter time) draws more current. Q/5 = slow discharge; 2Q = fast discharge."))
story.append(Spacer(1,0.15*cm))

story.append(Q("12. Define state of charge (SoC)."))
story.append(A())
story.append(P("<b>State of Charge (SoC)</b> is the present capacity of the battery — the amount of charge remaining after discharge from a top-of-charge (fully charged) condition."))
story.append(F("SoC_T(t) = Q_T - ∫[0 to t] i(τ)dτ         ...(4.2)"))
story.append(B("SoC = Q_T at full charge (t = 0)"))
story.append(B("SoC decreases as battery discharges"))
story.append(B("SoC = 0 at complete discharge"))
story.append(Note("Exam Tip: SoC = remaining charge. It decreases with time during discharge. SoC + SoD = Q_T."))
story.append(Spacer(1,0.15*cm))

story.append(Q("13. Define state of discharge (SoD)."))
story.append(A())
story.append(P("<b>State of Discharge (SoD)</b> is a measure of the charge that has been drawn from a battery since the last full charge."))
story.append(F("SoD_T(t) = ∫[0 to t] i(τ)dτ               ...(4.3)"))
story.append(F("Relation: SoC_T(t) = Q_T - SoD_T(t)"))
story.append(B("SoD = 0 at full charge"))
story.append(B("SoD increases as battery discharges"))
story.append(B("SoD = Q_T at complete discharge"))
story.append(Note("Exam Tip: SoD = charge drawn out of battery. SoD and SoC are complementary: SoC + SoD = Q_T."))
story.append(Spacer(1,0.15*cm))

story.append(Q("14. Define depth of discharge (DoD)."))
story.append(A())
story.append(P("<b>Depth of Discharge (DoD)</b> is the percentage of rated battery capacity to which a battery has been discharged."))
story.append(F("DoD(t) = [Q_T - SoC_T(t)] / Q_T  × 100%   ...(4.4)"))
story.append(F("DoD(t) = [∫i(τ)dτ / Q_T] × 100%"))
story.append(B("DoD = 0%: Fully charged battery"))
story.append(B("DoD = 100%: Completely discharged"))
story.append(B("Deep discharge: DoD ≥ 80% — withdrawal of at least 80% of rated capacity"))
story.append(Note("Exam Tip: DoD is a percentage. Deep discharge = DoD ≥ 80%. High DoD repeatedly degrades battery life."))
story.append(Spacer(1,0.15*cm))

# ── SECTION 3: Types of Batteries ────────────────────────────────────────────
story.append(SH("3. TYPES OF BATTERIES"))
story.append(HR())

story.append(Q("15. Name the different types of batteries used in electric vehicles."))
story.append(A())
story.append(P("The major types of rechargeable (secondary) batteries considered for EV and HEV applications are:"))
story.append(B("<b>Lead-Acid (Pb-acid)</b> — oldest, most widely used historically"))
story.append(B("<b>Nickel-Cadmium (NiCd)</b> — alkaline battery, good low-temperature performance"))
story.append(B("<b>Nickel-Metal-Hydride (NiMH)</b> — used in Toyota Prius HEV"))
story.append(B("<b>Lithium-Ion (Li-ion)</b> — high specific energy, dominant in modern EVs"))
story.append(B("<b>Lithium-Polymer (Li-poly)</b> — solid electrolyte, flexible shape"))
story.append(B("<b>Sodium-Sulphur (NaS)</b> — high energy, operates at 300°C"))
story.append(B("<b>Zinc-Air (Zn-Air)</b> — mechanically rechargeable, high specific energy"))
story.append(Note("Exam Tip: Know all 7 types. Li-ion is dominant in modern EVs; NiMH in older HEVs (Toyota Prius)."))
story.append(Spacer(1,0.15*cm))

story.append(Q("16. Why were lead-acid batteries widely used in EVs?"))
story.append(A())
story.append(P("Lead-acid batteries were widely used in EVs for the following reasons:"))
story.append(B("Relatively low cost — inexpensive to manufacture"))
story.append(B("Easy availability of raw materials (lead and sulphur)"))
story.append(B("Ease of manufacture — mature technology since 1859"))
story.append(B("High powered — can be designed to deliver high power"))
story.append(B("Safe and reliable — well understood technology"))
story.append(B("Recycling infrastructure already in place"))
story.append(B("Used extensively in industrial EVs: golf carts, airport vehicles, forklifts"))
story.append(Note("Exam Tip: Lead-acid = oldest, cheapest, safest — but low specific energy limits its use in modern EVs."))
story.append(Spacer(1,0.15*cm))

story.append(Q("17. List any two advantages of lead-acid batteries."))
story.append(A())
story.append(B("<b>1. Low Cost:</b> Lead-acid batteries are inexpensive to produce due to easy availability of lead and sulphur raw materials"))
story.append(B("<b>2. High Power:</b> Can be designed to deliver high specific power, making them suitable for high-current applications"))
story.append(B("Additional advantages: Safe, reliable, recyclable, mature manufacturing technology"))
story.append(Note("Exam Tip: For 2 marks, two clear points with brief explanation. Most common answer: low cost + safe/reliable."))
story.append(Spacer(1,0.15*cm))

story.append(Q("18. What are the limitations of lead-acid batteries for EV applications?"))
story.append(A())
story.append(B("<b>Low specific energy:</b> ~35 Wh/kg practical — severely limits EV range"))
story.append(B("<b>Poor cold temperature performance:</b> Capacity drops significantly at low temperatures"))
story.append(B("<b>Short calendar and cycle life:</b> Frequent replacement required"))
story.append(B("<b>Heavy weight:</b> Low specific energy means large mass for the energy required"))
story.append(B("<b>Sulphation:</b> PbSO4 buildup degrades capacity and performance over time"))
story.append(Note("Exam Tip: The PRIMARY limitation for EV use is LOW SPECIFIC ENERGY (only 35 Wh/kg vs 200 Wh/kg for Li-ion)."))
story.append(Spacer(1,0.15*cm))

story.append(Q("19. What is the nominal voltage of a Li-ion battery cell?"))
story.append(A())
story.append(P("The <b>nominal cell voltage of a Li-ion battery is 3.6 V</b>."))
story.append(B("This is equivalent to three NiMH or NiCd battery cells (which have ~1.2 V each)"))
story.append(B("Higher voltage per cell means fewer cells needed for a given pack voltage"))
story.append(B("This high voltage, combined with high specific energy, makes Li-ion ideal for EVs"))
story.append(Note("Exam Tip: Li-ion = 3.6 V/cell. NiMH/NiCd = 1.2 V/cell. Lead-acid = 2 V/cell."))
story.append(Spacer(1,0.15*cm))

story.append(Q("20. What are NiCd batteries?"))
story.append(A())
story.append(P("<b>Nickel-Cadmium (NiCd) batteries</b> are alkaline secondary batteries that use a nickel oxide positive electrode and a metallic cadmium negative electrode, with potassium hydroxide (KOH) as the electrolyte."))
story.append(F("Net reaction: Cd + 2NiOOH + 2H2O ⇌ 2Ni(OH)2 + 2Cd(OH)2  (1.299 V)"))
story.append(B("Practical cell voltage: 1.2 to 1.3 V"))
story.append(B("Specific energy: 30–50 Wh/kg (similar to lead-acid)"))
story.append(B("Advantages: Low temperature performance, flat discharge voltage, long life"))
story.append(B("Disadvantages: High cost, toxic cadmium, memory effect"))
story.append(Note("Exam Tip: NiCd = alkaline battery. Main drawback: toxic cadmium + high cost. Led to development of NiMH."))
story.append(Spacer(1,0.15*cm))

story.append(Q("21. What is the main advantage of Li-polymer batteries?"))
story.append(A())
story.append(P("The main advantage of Li-polymer batteries is their <b>potential for the highest specific energy and power</b> among all battery types, combined with the use of a <b>solid polymer electrolyte</b> which offers:"))
story.append(B("Highest potential specific energy and power"))
story.append(B("<b>Safety advantage:</b> Solid polymer replaces flammable liquid electrolyte — safer in EV accidents"))
story.append(B("<b>Flexible form factor:</b> Thin cell allows battery of any size or shape to fit EV chassis"))
story.append(B("Less reactive lithium (ionic form, intercalated into carbon)"))
story.append(Note("Exam Tip: Main advantage = solid polymer electrolyte → flexible shape + higher safety. Li-poly is the safest Li battery."))
story.append(Spacer(1,0.15*cm))

story.append(Q("22. What is the main disadvantage of Li-polymer batteries?"))
story.append(A())
story.append(P("The main disadvantage of Li-polymer batteries is the <b>need to operate in a high temperature range of 80°C to 120°C</b>."))
story.append(B("The solid polymer electrolyte only conducts ions above 60°C"))
story.append(B("Operating at 80–120°C requires a thermal management system"))
story.append(B("This adds cost, complexity, and weight to the system"))
story.append(B("Makes the battery unsuitable for applications where room-temperature operation is needed"))
story.append(Note("Exam Tip: Main disadvantage = high operating temperature (80–120°C). This limits its practical EV use."))
story.append(Spacer(1,0.15*cm))

story.append(Q("23. What is another name for sodium-metal chloride batteries?"))
story.append(A())
story.append(P("Sodium-metal chloride batteries are commonly known as <b>ZEBRA batteries</b>."))
story.append(B("ZEBRA = Zeolite Battery Research Africa Project"))
story.append(B("Originated from collaboration between UK and South African scientists in early 1980s"))
story.append(B("They use NiCl2 (or FeCl2) positive electrode, sodium negative electrode, beta-alumina electrolyte"))
story.append(B("ZEBRA batteries have been shown to be safe under all conditions of use"))
story.append(Note("Exam Tip: ZEBRA = sodium-nickel-chloride battery. Remember: NaCl (common salt) is used in assembly."))
story.append(Spacer(1,0.15*cm))

story.append(Q("24. What is the significance of a lithium-ion battery in electric vehicles?"))
story.append(A())
story.append(P("Lithium-ion batteries are of great significance in EVs because they offer the best combination of performance characteristics:"))
story.append(B("<b>High specific energy:</b> ~150–200 Wh/kg — longest EV range"))
story.append(B("<b>High specific power:</b> Supports rapid acceleration"))
story.append(B("<b>High energy efficiency:</b> Minimal energy loss during charge/discharge"))
story.append(B("<b>Good high-temperature performance</b>"))
story.append(B("<b>Low self-discharge rate</b>"))
story.append(B("<b>High nominal voltage:</b> 3.6 V/cell — fewer cells needed"))
story.append(B("<b>Recyclable components</b>"))
story.append(B("These characteristics make Li-ion the dominant battery technology in modern EVs (Tesla, Nissan Leaf, etc.)"))
story.append(Note("Exam Tip: Li-ion wins on specific energy + voltage + efficiency. Used in nearly all modern EV production."))
story.append(Spacer(1,0.15*cm))

story.append(Q("25. What is a lithium-ion battery, and why is it commonly used in electric vehicles?"))
story.append(A())
story.append(P("A <b>lithium-ion (Li-ion) battery</b> is a secondary battery that uses lithium intercalated carbon (graphite, LixC) as the negative electrode and a lithium metallic oxide (such as LiCoO2) as the positive electrode, with an organic electrolyte."))
story.append(Bold("Working Principle:"))
story.append(B("During discharge: Li+ ions travel from negative electrode (graphite) through electrolyte to positive electrode (LiCoO2)"))
story.append(B("During charge: Li+ ions move in reverse"))
story.append(F("Neg. electrode: Li_xC6 → 6C + xLi+ + xe-"))
story.append(F("Pos. electrode: xLi+ + xe- + Li(1-x)CoO2 → LiCoO2"))
story.append(Bold("Why used in EVs:"))
story.append(B("Highest specific energy among practical rechargeable batteries"))
story.append(B("3.6 V/cell nominal voltage — high energy per unit mass"))
story.append(B("Low self-discharge, good cycle life, recyclable"))
story.append(Note("Exam Tip: Intercalation = lithium ions inserted into/extracted from electrode crystal lattice. This is fully reversible."))
story.append(Spacer(1,0.15*cm))

# ── SECTION 4: Battery Modelling ─────────────────────────────────────────────
story.append(SH("4. MODELLING OF BATTERY"))
story.append(HR())

story.append(Q("26. What is the purpose of battery modelling in EVs?"))
story.append(A())
story.append(P("Battery modelling in EVs serves the following purposes:"))
story.append(B("<b>Range prediction:</b> The Fractional Depletion Model (FDM) is used to predict the driving range of an EV"))
story.append(B("<b>Battery management:</b> Helps determine SoC, SoD, DoD accurately during operation"))
story.append(B("<b>Design optimization:</b> Allows designers to select the right battery size and type for a given vehicle"))
story.append(B("<b>Peukert's equation:</b> Empirical model relating capacity, discharge current, and time — used in FDM"))
story.append(B("<b>Control system design:</b> Accurate battery model enables better energy management strategies"))
story.append(Note("Exam Tip: Battery modelling = Peukert's equation + FDM → to predict EV range and manage battery life."))
story.append(Spacer(1,0.15*cm))

# ── SECTION 5: Fuel Cells ─────────────────────────────────────────────────────
story.append(SH("5. FUEL CELL BASIC PRINCIPLE AND OPERATION"))
story.append(HR())

story.append(Q("27. What is a fuel cell?"))
story.append(A())
story.append(P("A <b>fuel cell</b> is an electrochemical device that produces electricity by means of a chemical reaction between a fuel (hydrogen) and an oxidant (oxygen), much like a battery. Unlike batteries, a fuel cell can produce electricity continuously as long as fuel is supplied."))
story.append(F("Anode:   H2 → 2H+ + 2e-"))
story.append(F("Cathode: 2e- + 2H+ + 1/2(O2) → H2O"))
story.append(F("Cell:    H2 + 1/2(O2) → H2O  + Electricity"))
story.append(B("Converts Gibbs free energy of chemical reaction into electrical energy"))
story.append(B("Only by-product is water — zero harmful emissions"))
story.append(Note("Exam Tip: Fuel cell = electrochemical device. Unlike a battery, it does NOT store energy — it GENERATES electricity from fuel. By-product = water only."))
story.append(Spacer(1,0.15*cm))

story.append(Q("28. What is the main difference between a battery and a fuel cell?"))
story.append(A())
diff_table = make_table(
    [Paragraph("<b>Battery</b>", S['body']), Paragraph("<b>Fuel Cell</b>", S['body'])],
    [
        [P("Stores chemical energy internally"), P("Converts fuel energy externally on demand")],
        [P("Has limited energy (finite charge)"), P("Produces electricity as long as fuel is supplied")],
        [P("Requires recharging when depleted"), P("Refuelling replaces recharging")],
        [P("Energy stored in electrodes"), P("Energy comes from fuel (H2) + oxidant (O2)")],
        [P("Electrodes are consumed/regenerated"), P("Electrodes act as catalysts, not consumed")],
    ],
    col_widths=[8.25*cm, 8.25*cm]
)
story.append(diff_table)
story.append(Note("Exam Tip: KEY DIFFERENCE: Battery = finite stored energy, needs recharging. Fuel cell = unlimited as long as fuel is supplied."))
story.append(Spacer(1,0.15*cm))

story.append(Q("29. Name the main components of a fuel cell."))
story.append(A())
story.append(B("<b>Anode:</b> Where hydrogen fuel is oxidized — releases H+ ions and electrons"))
story.append(B("<b>Cathode:</b> Where oxygen is reduced — combines H+, e-, and O2 to form water"))
story.append(B("<b>Electrolyte:</b> Allows H+ ions (protons) to flow from anode to cathode; blocks electrons"))
story.append(B("<b>Catalyst:</b> Speeds up the electrochemical reactions at both electrodes (typically platinum)"))
story.append(B("<b>External circuit:</b> Electrons flow from anode to cathode through external circuit, producing electricity"))
story.append(Note("Exam Tip: Remember all 5 components. Electron flow through external circuit = electricity. Ion flow through electrolyte = internal current."))
story.append(Spacer(1,0.15*cm))

story.append(Q("30. What fuels are commonly used in hydrogen fuel cells?"))
story.append(A())
story.append(B("<b>Pure Hydrogen (H2):</b> Most common, cleanest fuel — used in AFC and PEMFC"))
story.append(B("<b>Reformate (reformed natural gas, LNG):</b> Hydrogen extracted from natural gas — used in PAFC, MCFC, SOFC"))
story.append(B("<b>Methanol:</b> Used in DMFC (Direct Methanol Fuel Cell) — reformed internally to hydrogen"))
story.append(B("<b>Coal gas (H2 + CO):</b> Used in MCFC and SOFC"))
story.append(B("Hydrogen has the highest specific energy: 33,000 Wh/kg"))
story.append(Note("Exam Tip: For PEMFC — pure H2 and O2/air. For DMFC — methanol. For SOFC — H2, CO, natural gas."))
story.append(Spacer(1,0.15*cm))

story.append(Q("31. What is the function of the electrolyte in a fuel cell?"))
story.append(A())
story.append(P("The electrolyte in a fuel cell performs two critical functions:"))
story.append(B("<b>Ion conduction:</b> Allows H+ ions (protons) to migrate from the anode to the cathode to complete the internal circuit"))
story.append(B("<b>Electron insulation:</b> Blocks the flow of electrons, forcing them to travel through the external circuit and produce electrical energy"))
story.append(B("It is placed between the anode and cathode"))
story.append(B("Must have high ionic conductivity and zero electronic conductivity"))
story.append(B("Examples: KOH solution (AFC), Nafion polymer membrane (PEMFC), phosphoric acid (PAFC)"))
story.append(Note("Exam Tip: Electrolyte = ion conductor + electron insulator. This separation of electron and ion paths is what generates electricity."))
story.append(Spacer(1,0.15*cm))

story.append(Q("32. What is the role of a catalyst in a fuel cell?"))
story.append(A())
story.append(P("The <b>catalyst in a fuel cell speeds up the electrochemical reactions</b> at both the anode and cathode that would otherwise be too slow at low temperatures."))
story.append(B("At the anode: catalyst speeds up oxidation of hydrogen → H+ + e-"))
story.append(B("At the cathode: catalyst speeds up reduction of oxygen → combines with H+ and e- to form water"))
story.append(B("Platinum (Pt) is the most common catalyst — highly active but expensive"))
story.append(B("Catalyst is coated on a carbon support layer in contact with both electrode and electrolyte"))
story.append(B("Catalyst can be poisoned by CO (carbon monoxide) — reduces performance"))
story.append(Note("Exam Tip: Catalyst = platinum. It speeds reactions without being consumed. CO poisoning of Pt catalyst is a key challenge in PEMFCs."))
story.append(Spacer(1,0.15*cm))

story.append(Q("33. State any two applications of fuel cells."))
story.append(A())
story.append(B("<b>1. Electric Vehicles (EVs and HEVs):</b> PEMFC and AFC used as primary power source — produce electricity with only water as by-product"))
story.append(B("<b>2. Stationary Power Generation:</b> SOFC and MCFC used for large-scale stationary power plants with cogeneration capability"))
story.append(B("Other applications: NASA space shuttles (moon buggy), submarines, portable electronics, backup power"))
story.append(Note("Exam Tip: Two key applications = EVs/vehicles (PEMFC, AFC) + stationary power (SOFC, MCFC)."))
story.append(Spacer(1,0.15*cm))

# ── SECTION 6: Types of Fuel Cells ───────────────────────────────────────────
story.append(SH("6. TYPES OF FUEL CELLS"))
story.append(HR())

story.append(Q("34. Name the six major types of fuel cells."))
story.append(A())
story.append(B("<b>1. Alkaline Fuel Cell (AFC)</b> — KOH electrolyte, ~80°C"))
story.append(B("<b>2. Proton Exchange Membrane Fuel Cell (PEMFC)</b> — Nafion polymer, ~80°C"))
story.append(B("<b>3. Direct Methanol Fuel Cell (DMFC)</b> — methanol fuel, 90–120°C"))
story.append(B("<b>4. Phosphoric Acid Fuel Cell (PAFC)</b> — H3PO4 electrolyte, ~200°C"))
story.append(B("<b>5. Molten Carbonate Fuel Cell (MCFC)</b> — carbonate electrolyte, 600–700°C"))
story.append(B("<b>6. Solid Oxide Fuel Cell (SOFC)</b> — solid ceramic electrolyte, ~1000°C"))
story.append(Note("Exam Tip: Memorize all 6 with their abbreviations. PEMFC and AFC are for vehicles; SOFC and MCFC for stationary."))
story.append(Spacer(1,0.15*cm))

story.append(Q("35. What is the major limitation of fuel cells for EV applications?"))
story.append(A())
story.append(B("<b>High cost:</b> Expensive platinum catalyst and polymer membranes"))
story.append(B("<b>Infrastructure:</b> Hydrogen fuelling infrastructure is not yet widely available"))
story.append(B("<b>Start-up transient:</b> Fuel cells take time to start up and reach operating conditions"))
story.append(B("<b>Hydrogen storage:</b> Storing hydrogen safely and compactly on-board a vehicle is challenging"))
story.append(B("<b>Complexity:</b> Complex controller and water management system required"))
story.append(B("<b>Immaturity:</b> Technology not yet at commercial scale for high-volume EV production"))
story.append(Note("Exam Tip: Primary limitations = cost (Pt catalyst) + hydrogen infrastructure + start-up time + complexity."))
story.append(Spacer(1,0.15*cm))

# ── SECTION 7: PEMFC ──────────────────────────────────────────────────────────
story.append(SH("7. PEMFC AND ITS OPERATION"))
story.append(HR())

story.append(Q("36. What does PEMFC stand for?"))
story.append(A())
story.append(P("<b>PEMFC stands for Proton Exchange Membrane Fuel Cell.</b>"))
story.append(B("Also known as: Solid Polymer Membrane Fuel Cell"))
story.append(B("Uses a solid polymer membrane (Nafion) as the electrolyte"))
story.append(B("Operates at ~80°C (low temperature — suitable for vehicles)"))
story.append(B("Most investigated fuel cell for automotive applications"))
story.append(B("Developed first in 1960s for U.S. manned space program"))
story.append(Note("Exam Tip: PEMFC = Proton Exchange Membrane Fuel Cell. The 'proton exchange' refers to H+ ions passing through the Nafion membrane."))
story.append(Spacer(1,0.15*cm))

story.append(Q("37. What type of electrolyte is used in PEM fuel cells?"))
story.append(A())
story.append(P("PEM fuel cells use a <b>solid polymer membrane</b> as the electrolyte, specifically <b>perfluorosulfonic acid polymer</b>, commercially known as <b>Nafion (by DuPont)</b>."))
story.append(B("Nafion is an acidic polymer membrane — transports H+ (proton) ions"))
story.append(B("Solid electrolyte does not change, move, or vaporize from the cell"))
story.append(B("Must be kept humid for proper ion conductivity"))
story.append(B("Acts as both ionic conductor and gas separator between anode and cathode"))
story.append(Note("Exam Tip: PEMFC electrolyte = Nafion (perfluorosulfonic acid polymer). Key properties: solid, acidic, proton-conducting, humidity-sensitive."))
story.append(Spacer(1,0.15*cm))

story.append(Q("38. Name one advantage and disadvantage of PEMFC for EV applications."))
story.append(A())
story.append(Bold("Advantage:"))
story.append(B("<b>Low operating temperature (~80°C)</b> enables fast start-up — critical for vehicle applications"))
story.append(B("Highest power density among all fuel cell types (0.35–0.6 W/cm2)"))
story.append(B("Solid electrolyte — no liquid spillage, minimal corrosion risk"))
story.append(B("Can tolerate fuel impurities (unlike AFC which needs pure H2)"))
story.append(Bold("Disadvantage:"))
story.append(B("<b>Expensive platinum catalyst</b> is required — significantly raises cost"))
story.append(B("Catalyst is easily poisoned by carbon monoxide (CO)"))
story.append(B("Requires water management system — membrane must stay humid"))
story.append(Note("Exam Tip: Advantage = fast start-up + highest power density. Disadvantage = expensive Pt catalyst + CO poisoning."))
story.append(Spacer(1,0.15*cm))

# ── SECTION 8: Supercapacitors ────────────────────────────────────────────────
story.append(SH("8. SUPERCAPACITORS"))
story.append(HR())

story.append(Q("39. What is a supercapacitor?"))
story.append(A())
story.append(P("A <b>supercapacitor</b> is an advanced energy storage device that is a derivative of conventional capacitors, where energy density has been increased at the expense of power density to make the device function more like a battery."))
story.append(B("Uses an electrolyte that stores charge as ions (electrostatic) AND in porous carbon electrodes"))
story.append(B("No electrochemical reaction inside — energy stored electrostatically"))
story.append(B("Electrodes made of porous carbon with high internal surface area for high charge density"))
story.append(Bold("Key Characteristics:"))
story.append(B("Power density: ~10^6 W/m³"))
story.append(B("Energy density: ~10^4 Wh/m³"))
story.append(B("Discharge time: ~110 s (much faster than batteries ~5000 s)"))
story.append(B("Cycle life: ~10^5 cycles (much higher than batteries: 100–1000 cycles)"))
story.append(Note("Exam Tip: Supercapacitor = high power + fast discharge + long cycle life + low energy. Bridges gap between capacitor and battery."))
story.append(Spacer(1,0.15*cm))

story.append(Q("40. Justify the use of supercapacitors in hybrid braking systems."))
story.append(A())
story.append(P("Supercapacitors are highly suitable for use in hybrid braking systems (regenerative braking) for the following reasons:"))
story.append(B("<b>High power density:</b> Can absorb large amounts of energy in very short time during braking — batteries cannot charge fast enough"))
story.append(B("<b>Fast charging/discharging:</b> Regenerative braking delivers a large power pulse (~110 s discharge time) — perfectly suited to supercapacitor characteristics"))
story.append(B("<b>Long cycle life:</b> ~10^5 cycles — can withstand thousands of braking events unlike batteries"))
story.append(B("<b>High efficiency:</b> Energy storage and retrieval are highly efficient (no chemical reaction losses)"))
story.append(B("<b>Transient power supply:</b> Stored energy can be used during rapid acceleration or hill climbing"))
story.append(B("<b>Battery protection:</b> Used alongside batteries to handle transient peaks, extending battery life"))
story.append(P("In EV systems, supercapacitors are used as intermediate energy transfer devices in conjunction with batteries or fuel cells — they handle sudden power demands (acceleration, hill climbing) while batteries handle steady-state energy supply."))
story.append(Note("Exam Tip: Key justification = HIGH POWER DENSITY + FAST RESPONSE + LONG CYCLE LIFE. These three properties make supercapacitors ideal for regenerative braking."))

story.append(PageBreak())

# ═══════════════════════════════════════════════════════════════════════════
# PART B — 8/10 MARK QUESTIONS
# ═══════════════════════════════════════════════════════════════════════════
story.append(section_banner("MODULE 4 — PART B: 8/10-MARK QUESTIONS"))
story.append(Spacer(1, 0.3*cm))

# ── SECTION 1 ─────────────────────────────────────────────────────────────────
story.append(SH("1. ENERGY STORAGE REQUIREMENTS"))
story.append(HR())

story.append(Q("1. Explain the energy storage requirements of electric vehicles and hybrid electric vehicles."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("The energy storage system is the most critical and challenging subsystem in the design of EVs and HEVs. It must provide sufficient energy and power while meeting strict constraints of size, weight, cost, safety, and life."))

story.append(Bold("Basic Energy Requirement:"))
story.append(P("A basic requirement for EVs is a portable source of electrical energy, converted to mechanical energy in the electric motor. Sources include batteries, fuel cells, and flywheels."))

story.append(Bold("Desirable Properties of EV/HEV Energy Storage:"))
story.append(B("<b>High Specific Energy (Wh/kg):</b> More energy per unit mass → longer range between charges"))
story.append(B("<b>High Specific Power (W/kg):</b> More power per unit mass → better acceleration and performance"))
story.append(B("<b>High Charge Acceptance Rate:</b> Must absorb energy quickly during regenerative braking"))
story.append(B("<b>Long Calendar and Cycle Life:</b> Must last years of service with thousands of charge/discharge cycles"))
story.append(B("<b>Safety:</b> Must be safe under all operating conditions including accidents"))
story.append(B("<b>Low Cost:</b> Must be commercially affordable for mass production"))
story.append(B("<b>Recyclability:</b> Environmental responsibility after end of life"))
story.append(B("<b>Thermal Management:</b> Must operate reliably across temperature range"))

story.append(Bold("Comparison of Energy Sources:"))
energy_table = make_table(
    [Paragraph("<b>Energy Source</b>", S['body']), Paragraph("<b>Specific Energy (Wh/kg)</b>", S['body'])],
    [
        [P("Gasoline"), P("12,500")],
        [P("Hydrogen"), P("33,000")],
        [P("Natural gas"), P("9,350")],
        [P("Lead-acid battery"), P("35")],
        [P("Lithium-polymer battery"), P("200")],
        [P("Flywheel (carbon-fiber)"), P("200")],
    ],
    col_widths=[9.5*cm, 7*cm]
)
story.append(energy_table)

story.append(Bold("Additional Technical Challenges:"))
story.append(B("Battery balancing: electrically and thermally balancing cells within a pack"))
story.append(B("Accurate SoC estimation techniques"))
story.append(B("Recycling infrastructure for battery components"))
story.append(B("Cost reduction for high-volume production"))

story.append(Bold("Current Status:"))
story.append(P("Battery technology has undergone extensive R&D for 30+ years, yet no single battery delivers an acceptable combination of power, energy, and life cycle for high-volume production. This remains the biggest impediment in commercializing EVs and HEVs."))

story.append(Bold("Solution — HEVs:"))
story.append(P("A near-term solution for minimizing environmental pollution, in the absence of a suitable high-energy-density battery, is the Hybrid Electric Vehicle (HEV) that combines propulsion from gasoline engines and electric motors."))
story.append(Note("Exam Tip: Key requirements = high specific energy + high specific power + long life + fast charge acceptance + low cost. Mention the gap between theoretical and practical specific energy."))
story.append(Spacer(1,0.2*cm))

story.append(Q("2. Explain the construction and components of a battery cell with a neat diagram."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("A battery cell is the fundamental electrochemical unit that converts stored chemical energy into electrical energy on demand. One or more cells connected in series form a battery module."))

story.append(Bold("Hierarchy:"))
story.append(F("Unit Cell  →  Battery Module (cells + casing)  →  Battery Pack (modules series-parallel)"))

story.append(Bold("Components of a Battery Cell:"))

story.append(Bold("1. Positive Electrode:"))
story.append(B("An oxide, sulphide, or other compound capable of being reduced during discharge"))
story.append(B("Consumes electrons from the external circuit during discharge"))
story.append(B("Examples: Lead oxide (PbO2) in lead-acid; Nickel oxyhydroxide (NiOOH) in NiCd"))
story.append(B("Material is in solid state; highly porous structure to maximize surface area"))

story.append(Bold("2. Negative Electrode:"))
story.append(B("A metal or alloy capable of being oxidized during discharge"))
story.append(B("Generates electrons into the external circuit during discharge"))
story.append(B("Examples: Lead (Pb) in lead-acid; Cadmium (Cd) in NiCd; Carbon/graphite (LixC6) in Li-ion"))
story.append(B("Also in solid state within the battery cell"))

story.append(Bold("3. Electrolyte:"))
story.append(B("Medium that permits ionic conduction between the two electrodes"))
story.append(B("Must have high ionic conductivity and zero electronic conductivity (to prevent self-discharge)"))
story.append(B("May be liquid (lead-acid, NiCd), gel/paste (NiMH, Li-ion), or solid (Li-polymer)"))
story.append(B("Can be acidic (lead-acid: H2SO4) or alkaline (NiCd, NiMH: KOH)"))

story.append(Bold("4. Separator:"))
story.append(B("Electrically insulating layer that physically separates electrodes of opposite polarity"))
story.append(B("Must be permeable to electrolyte ions"))
story.append(B("May store or immobilize the electrolyte"))
story.append(B("Made from synthetic polymers"))

story.append(Bold("5. Battery Terminals:"))
story.append(B("Connection points between electrodes and external circuit"))
story.append(B("Positive terminal: connected to positive electrode"))
story.append(B("Negative terminal: connected to negative electrode"))

story.append(Bold("ASCII Diagram of Battery Cell:"))
cell_diag = [
    "  External Circuit (Load R_L)",
    "  ┌─────────────────────────────┐",
    "  │   Electron Flow →           │",
    "  │ (+)Terminal       (-)Terminal│",
    "  │  │                    │     │",
    "  │  ↓                    ↑     │",
    "  ┌──┴────┬──────────────┬─┴────┐",
    "  │  POS  │  SEPARATOR   │ NEG  │",
    "  │ELECTR │  (insulator) │ELECTR│",
    "  │(PbO2) │ ←── Ions ── │(Pb)  │",
    "  │  (+)  │  Electrolyte │ (-)  │",
    "  └───────┴──────────────┴──────┘",
    "  Positive Electrode  Negative Electrode",
    "   (reduction)          (oxidation)",
]
story.append(MonoBlock(cell_diag))

story.append(Bold("Energy Conversion:"))
story.append(P("The energy stored in a battery = difference in free energy between charged and discharged chemical states. Chemical oxidation at negative electrode releases electrons; reduction at positive electrode consumes electrons. The flow of electrons through the external circuit IS the electrical current."))

story.append(Bold("Self-Discharge:"))
story.append(P("In ideal batteries, current flows only when external circuit is complete. In practice, diffusion effects cause slow self-discharge even with open circuit. This is an important descriptor of battery quality."))
story.append(Note("Exam Tip: Draw the cell diagram clearly showing: (+) electrode, (-) electrode, separator, electrolyte, and electron flow direction. Know the role of each component."))
story.append(Spacer(1,0.2*cm))

story.append(Q("3. Illustrate how chemical energy stored in batteries is converted into electrical energy for vehicle propulsion."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("A battery stores energy as chemical energy in the form of chemical potential difference between the positive and negative electrode materials. When connected to a load (electric motor), this chemical energy is released as electrical energy through electrochemical reactions."))

story.append(Bold("Step-by-Step Energy Conversion Process:"))

story.append(Bold("Step 1: Chemical Energy Storage (Charged State)"))
story.append(P("In the fully charged state, the positive and negative electrode materials are in their high-energy chemical forms. For lead-acid: PbO2 (positive) and Pb (negative) are the stored reactants."))

story.append(Bold("Step 2: Oxidation at Negative Electrode (Anode)"))
story.append(P("When connected to load, chemical oxidation occurs at the negative electrode:"))
story.append(F("Pb(s) + SO4²⁻(aq) → PbSO4(s) + 2e⁻     [Lead-acid example]"))
story.append(P("Electrons are released into the external circuit. This is oxidation — loss of electrons."))

story.append(Bold("Step 3: Electron Flow Through External Circuit"))
story.append(P("The released electrons flow from the negative terminal through the external circuit (motor controller, motor) to the positive terminal. This flow of electrons IS the electric current that does work."))
story.append(F("Electric current I = dq/dt = rate of electron flow"))

story.append(Bold("Step 4: Reduction at Positive Electrode (Cathode)"))
story.append(P("At the positive electrode, electrons from the external circuit are consumed in a reduction reaction:"))
story.append(F("PbO2(s) + 4H⁺ + SO4²⁻ + 2e⁻ → PbSO4(s) + 2H2O    [Lead-acid]"))

story.append(Bold("Step 5: Ion Transport Through Electrolyte"))
story.append(P("Internally, ions (SO4²⁻, H+) flow through the electrolyte between the electrodes to complete the internal circuit. Without this ionic conduction, the external electron flow would stop."))

story.append(Bold("Step 6: Electrical Energy to Mechanical Energy"))
story.append(P("The electrical energy delivered by the battery powers the motor controller (inverter), which drives the electric motor, converting electrical energy to mechanical energy for vehicle propulsion:"))
story.append(F("Chemical Energy → Electrical Energy → Mechanical Energy (Propulsion)"))

story.append(Bold("Overall Discharge Reaction (Lead-Acid):"))
story.append(F("Pb(s) + PbO2(s) + 4H⁺ + SO4²⁻ → 2PbSO4(s) + 2H2O + Electrical Energy"))

story.append(Bold("Recharging (Reverse Process):"))
story.append(P("During regenerative braking or external charging, electrical energy is pushed back into the battery, reversing the chemical reactions and restoring the original chemical energy state."))
story.append(F("Electrical Energy → Chemical Energy (stored)"))

energy_flow = [
    "  BATTERY (Chemical Energy)                    MOTOR",
    "  ┌─────────────────────┐                ┌──────────────┐",
    "  │ + PbO2 | Pb - (neg) │ ──electrons──→ │ Motor Drive  │",
    "  │       Electrolyte    │               │  Controller  │",
    "  │      (H2SO4 aq)      │ ←── ions ─── │  (Inverter)  │",
    "  └─────────────────────┘                └──────┬───────┘",
    "                                                │",
    "                                         ┌──────▼───────┐",
    "                                         │ Electric     │",
    "                                         │  Motor       │",
    "                                         └──────┬───────┘",
    "                                                │",
    "                                         Mechanical Energy",
    "                                         (Vehicle Propulsion)",
]
story.append(MonoBlock(energy_flow))
story.append(Note("Exam Tip: Draw the complete energy flow chain. Emphasize that oxidation at negative electrode generates electrons; reduction at positive electrode consumes them. Ion flow through electrolyte completes the internal circuit."))
story.append(Spacer(1,0.2*cm))

story.append(Q("4. Analyze the limitations of existing battery technologies for high-volume EV production."))
story.append(A())
story.append(Bold("Overview:"))
story.append(P("Despite 30+ years of R&D, no battery technology currently delivers an acceptable combination of power, energy, life, safety, and cost for high-volume EV production."))

story.append(Bold("Analysis of Limitations by Category:"))
story.append(Bold("1. Specific Energy Limitation:"))
story.append(P("Practical specific energy of batteries is far below theoretical values and far below gasoline (12,500 Wh/kg):"))
limits_table = make_table(
    [Paragraph("<b>Battery</b>", S['body']), Paragraph("<b>Theoretical (Wh/kg)</b>", S['body']),
     Paragraph("<b>Practical (Wh/kg)</b>", S['body']), Paragraph("<b>Gap</b>", S['body'])],
    [
        [P("Lead-acid"), P("108"), P("35–50"), P("54–67% loss")],
        [P("NiCd"), P("—"), P("30–50"), P("Low practical")],
        [P("NiMH"), P("—"), P("60–80"), P("Moderate")],
        [P("Li-ion"), P("—"), P("150–200"), P("Best practical")],
        [P("Li-polymer"), P("—"), P("200"), P("High temp needed")],
        [P("NaS"), P("770"), P("150–300"), P("Safety issues")],
    ],
    col_widths=[3.5*cm, 4.5*cm, 4.5*cm, 3.5*cm]
)
story.append(limits_table)

story.append(Bold("2. Cost Limitation:"))
story.append(B("Battery packs are too expensive for mass-market vehicles"))
story.append(B("Li-ion: expensive cobalt oxide positive electrode and manufacturing"))
story.append(B("PEMFC: platinum catalyst cost is prohibitive"))
story.append(B("Short calendar life increases lifetime ownership cost"))

story.append(Bold("3. Cycle and Calendar Life:"))
story.append(B("Lead-acid: short cycle life — needs frequent replacement"))
story.append(B("NiCd: cadmium toxicity + insufficient power for EVs"))
story.append(B("Most batteries degrade significantly after 300–1000 deep discharge cycles"))
story.append(B("High-temperature operation (NaS, NaMCl): thermal management system required"))

story.append(Bold("4. Safety Concerns:"))
story.append(B("NaS: molten sodium-sulphur reaction risk — explosive in accidents"))
story.append(B("Li-ion: thermal runaway risk if overcharged or damaged"))
story.append(B("Lead-acid: hydrogen gas buildup in sealed configurations"))

story.append(Bold("5. Temperature Sensitivity:"))
story.append(B("Lead-acid: poor cold temperature performance"))
story.append(B("Li-polymer: needs 80–120°C operation — impractical without heater"))
story.append(B("NaS: needs ~300°C — large thermal insulation required"))

story.append(Bold("6. Manufacturing and Infrastructure:"))
story.append(B("Scaling from small batteries (phones) to EV packs is technically challenging"))
story.append(B("Battery pack thermal and electrical balancing is complex"))
story.append(B("Recycling infrastructure not fully in place for advanced batteries"))

story.append(Bold("Conclusion:"))
story.append(P("The gap between theoretical and practical performance, combined with high cost and safety concerns, makes batteries the single biggest impediment in commercializing EVs and HEVs for high-volume production."))
story.append(Note("Exam Tip: Structure the answer as: specific energy limitation + cost + life + safety + temperature + manufacturing. Use the table to show theoretical vs practical gap."))
story.append(Spacer(1,0.2*cm))

# ── SECTION 2: Battery Parameters ────────────────────────────────────────────
story.append(SH("2. BATTERY PARAMETERS"))
story.append(HR())

story.append(Q("5. Explain the concept of battery capacity and theoretical capacity of a battery."))
story.append(A())
story.append(Bold("Definition:"))
story.append(P("Battery capacity is the total amount of free charge that can be generated by the active material at the negative electrode and consumed by the positive electrode during complete discharge."))

story.append(Bold("Unit:"))
story.append(F("Battery capacity is measured in Ampere-hours (Ah)"))
story.append(F("1 Ah = 3600 Coulombs (C)"))
story.append(F("1 C = charge transferred by 1 A current in 1 second"))

story.append(Bold("Theoretical Capacity Formula:"))
story.append(F("Q_T = x · n · F   [Coulombs]"))
story.append(Bold("Where:"))
story.append(B("x = moles of limiting reactant = m_R / M_m"))
story.append(B("m_R = mass of limiting reactant [kg]"))
story.append(B("M_m = molar mass of limiting reactant [g/mol]"))
story.append(B("n = number of electrons produced by the negative electrode discharge reaction"))
story.append(B("F = Faraday's constant = L × e0 = 6.022045×10²³ × 1.6021892×10⁻¹⁹ = 96,485 C/mol"))
story.append(B("L = Avogadro constant; e0 = electron charge"))

story.append(Bold("Theoretical Capacity in Ah:"))
story.append(F("Q_T = 0.278 · F · (m_R · n / M_m)   [Ah]         ...(4.1)"))

story.append(Bold("Battery Cells in Series:"))
story.append(P("When cells are connected in series (as in a battery), the total capacity is determined by the smallest cell capacity:"))
story.append(F("Q_T(battery) = Q_T(cell)   [for series connection]"))

story.append(Bold("Practical vs Theoretical Capacity:"))
story.append(P("Practical capacity is always less than theoretical due to internal resistance, electrode passivation (PbSO4 buildup), electrolyte depletion, and non-uniform current distribution."))

cap_diag = [
    "  Capacity Measurement Circuit:",
    "  ┌───────────────────────────────────┐",
    "  │      Load (R_L) ← i(t)           │",
    "  │                                  │",
    "  │(+)──────────────────────────(-)  │",
    "  │         Battery Q_T               │",
    "  └───────────────────────────────────┘",
    "  Battery discharged at constant current",
    "  until terminal voltage drops to cut-off",
    "  voltage V_cut",
]
story.append(MonoBlock(cap_diag))
story.append(Note("Exam Tip: Theoretical capacity formula Q_T = x·n·F is important. Know each variable. Practical capacity < theoretical always."))
story.append(Spacer(1,0.2*cm))

story.append(Q("6. Explain the concept of battery discharge rate with suitable examples."))
story.append(A())
story.append(Bold("Definition:"))
story.append(P("<b>Discharge rate</b> is the current at which a battery is discharged. It is expressed as Q/h rate, where Q is the rated battery capacity and h is the discharge time in hours."))
story.append(F("Discharge rate = Q_T / t   [Amperes]"))
story.append(B("Q_T = rated battery capacity [Ah]"))
story.append(B("t = discharge time [hours]"))

story.append(Bold("Notation:"))
story.append(B("1Q = rated capacity (100% capacity reference)"))
story.append(B("Q/5 rate or 0.2Q rate = discharge at 1/5 the rated capacity per hour"))
story.append(B("2Q rate = discharge at twice the rated capacity per hour"))

story.append(Bold("Example (Q_T = 100 Ah):"))
story.append(F("Q/5 rate  (0.2Q): 100 Ah / 5 h  = 20 A   (slow discharge over 5 hours)"))
story.append(F("Q/2 rate  (0.5Q): 100 Ah / 2 h  = 50 A"))
story.append(F("1Q rate:          100 Ah / 1 h  = 100 A  (full capacity in 1 hour)"))
story.append(F("2Q rate:          100 Ah / 0.5 h = 200 A  (fast discharge in 30 min)"))

story.append(Bold("Effect of Discharge Rate on Capacity (Peukert Effect):"))
story.append(P("Higher discharge current reduces the practical capacity of a battery. A battery discharged quickly delivers LESS total energy than one discharged slowly. This is captured by Peukert's equation:"))
story.append(F("I^n × t_cut = λ   (Peukert's equation)"))
story.append(F("Practical capacity: Q = λ / I^(n-1)"))
story.append(P("Since n > 1: higher I → lower Q (less capacity available at high discharge rates)."))

rate_table = make_table(
    [Paragraph("<b>Discharge Rate</b>", S['body']), Paragraph("<b>Current (for 100Ah)</b>", S['body']),
     Paragraph("<b>Time</b>", S['body']), Paragraph("<b>Application</b>", S['body'])],
    [
        [P("Q/5 (0.2Q)"), P("20 A"), P("5 hours"), P("Gentle driving, city use")],
        [P("1Q"), P("100 A"), P("1 hour"), P("Normal operation")],
        [P("2Q"), P("200 A"), P("30 min"), P("Aggressive acceleration")],
        [P("5Q"), P("500 A"), P("12 min"), P("Peak acceleration, rapid discharge")],
    ],
    col_widths=[4*cm, 4.5*cm, 3*cm, 5*cm]
)
story.append(rate_table)
story.append(Note("Exam Tip: Know the formula: Discharge Rate = Q_T/t. And the numerical example. Mention Peukert effect: higher rate → lower practical capacity."))
story.append(Spacer(1,0.2*cm))

story.append(Q("7. Explain the state of charge (SoC) and its significance in battery operation."))
story.append(A())
story.append(Bold("Definition:"))
story.append(P("<b>State of Charge (SoC)</b> is the present charge capacity of the battery — the amount of charge that remains after discharge from a fully charged (top-of-charge) condition."))

story.append(Bold("Mathematical Expression:"))
story.append(P("The current i(t) is the rate of change of charge:"))
story.append(F("i(t) = dq/dt"))
story.append(P("For a small interval dt, the change in SoC is:"))
story.append(F("dSoC_T = -dq = -i(t)dt"))
story.append(P("Integrating from initial time t0 (fully charged, SoC = Q_T) to time t:"))
story.append(F("SoC_T(t) = Q_T - ∫[0 to t] i(τ)dτ             ...(4.2)"))

story.append(Bold("Boundary Conditions:"))
story.append(F("At t = 0 (fully charged): SoC_T(t0) = Q_T"))
story.append(F("At complete discharge:     SoC_T = 0"))
story.append(F("SoC = Q_T - SoD_T(t)"))

story.append(Bold("SoC Measurement Circuit:"))
soc_diag = [
    "  ┌─────────────────────────────────────┐",
    "  │ Load  ←── Current Sensor ←── i(t)  │",
    "  │  (Motor Drive)                      │",
    "  │                                     │",
    "  │ (+) ─────────────────────────── (-) │",
    "  │              Battery Q_T             │",
    "  └─────────────────────────────────────┘",
    "  Current sensor measures i(t)",
    "  SoC computed by integration: Q_T - ∫i(t)dt",
]
story.append(MonoBlock(soc_diag))

story.append(Bold("Significance of SoC in Battery Operation:"))
story.append(B("<b>Range estimation:</b> SoC tells the driver how much energy remains → predicts remaining driving range"))
story.append(B("<b>Charging control:</b> SoC determines when charging should begin and end"))
story.append(B("<b>Battery protection:</b> Prevents overdischarge (SoC → 0) which damages battery"))
story.append(B("<b>Regenerative braking control:</b> If SoC is high, regenerative braking must be limited"))
story.append(B("<b>Energy management:</b> Battery management system (BMS) uses SoC to optimize energy usage"))
story.append(B("<b>Thermal management:</b> SoC affects internal resistance and heat generation"))
story.append(Note("Exam Tip: SoC = remaining charge. SoC + SoD = Q_T. Know the integral formula. SoC is critical for all BMS functions."))
story.append(Spacer(1,0.2*cm))

story.append(Q("8. Explain the state of discharge (SoD) and depth of discharge (DoD) of a battery."))
story.append(A())
story.append(Bold("State of Discharge (SoD):"))
story.append(P("SoD is a measure of the charge that has been drawn from a battery since the last full charge."))
story.append(F("SoD_T(t) = ∫[0 to t] i(τ)dτ                  ...(4.3)"))
story.append(B("SoD = 0 at full charge"))
story.append(B("SoD increases as battery discharges"))
story.append(B("SoD = Q_T at complete discharge"))
story.append(F("Relation: SoC_T(t) = Q_T - SoD_T(t)"))

story.append(Bold("Depth of Discharge (DoD):"))
story.append(P("DoD is the percentage of rated capacity to which a battery has been discharged."))
story.append(F("DoD(t) = [Q_T - SoC_T(t)] / Q_T × 100%        ...(4.4)"))
story.append(F("DoD(t) = [∫i(τ)dτ / Q_T] × 100%"))
story.append(B("DoD = 0%: Battery fully charged"))
story.append(B("DoD = 50%: Half the capacity used"))
story.append(B("DoD = 100%: Battery completely discharged"))
story.append(B("Deep discharge: DoD ≥ 80% — withdrawal of at least 80% of rated capacity"))

story.append(Bold("Relationship Summary:"))
rel_table = make_table(
    [Paragraph("<b>Parameter</b>", S['body']), Paragraph("<b>At Full Charge</b>", S['body']),
     Paragraph("<b>At 50% Used</b>", S['body']), Paragraph("<b>At Empty</b>", S['body'])],
    [
        [F("SoC"), F("Q_T (100%)"), F("0.5Q_T (50%)"), F("0 (0%)")],
        [F("SoD"), F("0 (0%)"), F("0.5Q_T (50%)"), F("Q_T (100%)")],
        [F("DoD"), F("0%"), F("50%"), F("100%")],
    ],
    col_widths=[3.5*cm, 4.5*cm, 4.5*cm, 4*cm]
)
story.append(rel_table)

story.append(Bold("Significance of DoD:"))
story.append(B("Determines battery cycle life — deeper DoD per cycle reduces total number of cycles"))
story.append(B("EV batteries typically limited to 80% DoD to preserve cycle life"))
story.append(B("Used in the Fractional Depletion Model (FDM) to predict EV range"))
story.append(B("Controls charging/discharging protocols in the Battery Management System (BMS)"))
story.append(Note("Exam Tip: SoD = charge drawn. DoD = percentage discharged. Know all three formulas. Deep discharge = DoD ≥ 80%."))
story.append(Spacer(1,0.2*cm))

# ── SECTION 3: Types of Batteries ────────────────────────────────────────────
story.append(SH("3. TYPES OF BATTERIES"))
story.append(HR())

story.append(Q("9. Explain the construction and working principle of a lead-acid battery."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("The lead-acid battery is the oldest and most widely used battery type, with a history dating to 1859. It operates through reversible electrochemical reactions between lead (Pb) and lead dioxide (PbO2) in a sulfuric acid electrolyte."))

story.append(Bold("Construction:"))
story.append(B("<b>Positive Electrode:</b> Lead oxide (PbO2) — highly porous lattice grid structure to maximize surface area (50–150 m²/Ah)"))
story.append(B("<b>Negative Electrode:</b> Spongy lead (Pb) — pasted type construction"))
story.append(B("<b>Electrolyte:</b> Aqueous sulfuric acid [H2SO4(aq)]"))
story.append(B("<b>Separator:</b> Porous synthetic polymer — separates electrodes, allows ion flow"))
story.append(B("<b>Grid:</b> Lead alloy lattice framework to hold active material"))
story.append(B("<b>Cell container:</b> Polypropylene or similar casing"))

story.append(Bold("Cell Discharge Operation:"))
story.append(F("Positive electrode: PbO2(s) + 4H⁺ + SO4²⁻ + 2e⁻ → PbSO4(s) + 2H2O"))
story.append(F("Negative electrode: Pb(s) + SO4²⁻ → PbSO4(s) + 2e⁻"))
story.append(F("Overall discharge: Pb + PbO2 + 4H⁺ + SO4²⁻ → 2PbSO4 + 2H2O"))
story.append(P("During discharge: electrons flow from negative electrode through external circuit to positive electrode. Current flows OUT of positive terminal."))

story.append(Bold("Cell Charge Operation (Reverse):"))
story.append(F("Positive electrode: PbSO4(s) + 2H2O → PbO2(s) + 4H⁺ + SO4²⁻ + 2e⁻"))
story.append(F("Negative electrode: PbSO4(s) + 2e⁻ → Pb(s) + SO4²⁻"))
story.append(F("Overall charge: 2PbSO4 + 2H2O → Pb + PbO2 + 2H2SO4"))
story.append(P("During charging: external current forces electrons in reverse direction, regenerating Pb and PbO2 from PbSO4."))

story.append(Bold("ASCII Diagram:"))
pb_diag = [
    "  DISCHARGE (Battery → Motor):                CHARGE (Charger → Battery):",
    "  Electron flow →                             ← Electron flow",
    "  ┌──────────────┬────────────────┐           ┌──────────────┬────────────┐",
    "  │  PbO2  (+)   │  Pb (-)        │           │  PbSO4(+)   │  PbSO4(-)  │",
    "  │  ↓reduction  │  oxidation↑    │           │  ↑oxidation  │  reduction↓│",
    "  │  PbSO4       │  PbSO4         │           │  PbO2        │  Pb        │",
    "  └──────────────┴────────────────┘           └──────────────┴────────────┘",
    "       H2SO4 electrolyte (ion transport)             H2SO4 electrolyte",
    "       H⁺ and SO4²⁻ migrate between electrodes",
]
story.append(MonoBlock(pb_diag))

story.append(Bold("Key Performance Data:"))
story.append(B("Nominal cell voltage: 2 V/cell (6 cells = 12 V battery)"))
story.append(B("Practical specific energy: 35–50 Wh/kg"))
story.append(B("Advantages: Low cost, safe, reliable, high power, recyclable"))
story.append(B("Disadvantages: Low specific energy, poor cold performance, short cycle life, sulphation"))

story.append(Bold("Sulphation Issue:"))
story.append(P("PbSO4 deposited in dense fine-grain form on electrodes (sulphation) inhibits discharge reactions, reduces capacity, and degrades performance. This is the primary failure mode."))
story.append(Note("Exam Tip: Draw both discharge and charge diagrams. Write all chemical reactions. Mention sulphation as key issue."))
story.append(Spacer(1,0.2*cm))

story.append(Q("10. Explain the construction and characteristics of Nickel-Cadmium batteries."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("Nickel-Cadmium (NiCd) is an alkaline secondary battery that derives electrical energy from the reaction of cadmium with nickel oxide in a KOH electrolyte."))

story.append(Bold("Construction:"))
story.append(B("<b>Positive Electrode:</b> Nickel oxyhydroxide (NiOOH) — nickel oxide"))
story.append(B("<b>Negative Electrode:</b> Metallic cadmium (Cd)"))
story.append(B("<b>Electrolyte:</b> Aqueous potassium hydroxide (KOH) — alkaline"))
story.append(B("<b>Separator:</b> Synthetic polymer — permeable to OH⁻ ions"))

story.append(Bold("Electrochemical Reactions:"))
story.append(F("Net reaction: Cd + 2NiOOH + 2H2O ⇌ 2Ni(OH)2 + 2Cd(OH)2  (1.299 V)"))
story.append(F("Practical cell voltage: 1.2 to 1.3 V"))

story.append(Bold("Key Characteristics:"))
nicd_table = make_table(
    [Paragraph("<b>Parameter</b>", S['body']), Paragraph("<b>Value/Description</b>", S['body'])],
    [
        [P("Cell voltage"), P("1.2–1.3 V")],
        [P("Specific energy"), P("30–50 Wh/kg (similar to lead-acid)")],
        [P("Electrolyte"), P("KOH (alkaline)")],
        [P("Low-temperature performance"), P("Superior to lead-acid")],
        [P("Discharge characteristic"), P("Flat discharge voltage — consistent output")],
        [P("Cycle life"), P("Long life, excellent reliability")],
        [P("Maintenance"), P("Low maintenance required")],
    ],
    col_widths=[6*cm, 10.5*cm]
)
story.append(nicd_table)

story.append(Bold("Advantages:"))
story.append(B("Superior low-temperature performance compared to lead-acid"))
story.append(B("Flat discharge voltage — consistent power delivery"))
story.append(B("Long cycle life and excellent reliability"))
story.append(B("Low maintenance requirements"))

story.append(Bold("Disadvantages:"))
story.append(B("High cost compared to lead-acid"))
story.append(B("Toxic cadmium — serious environmental concern"))
story.append(B("Memory effect: must be fully discharged before recharging for maximum capacity"))
story.append(B("Insufficient power delivered for demanding EV applications"))
story.append(B("Led to development of NiMH batteries as a more suitable alternative"))
story.append(Note("Exam Tip: NiCd = alkaline, KOH electrolyte. Main issues: toxic Cd + memory effect + high cost. These drove development of NiMH."))
story.append(Spacer(1,0.2*cm))

story.append(Q("11. Explain the working principle of Nickel-Metal Hydride batteries."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("The Nickel-Metal-Hydride (NiMH) battery is a successor to the nickel-hydrogen battery and is already in use in production HEVs (Toyota Prius, Chrysler Epic). It stores hydrogen in a metallic alloy as the negative electrode."))

story.append(Bold("Working Principle — Key Concept:"))
story.append(P("NiMH batteries are based on the fact that fine particles of certain metallic alloys absorb large quantities of hydrogen at certain pressures and temperatures to form metal-hydride compounds. These metal hydrides can absorb and release hydrogen repeatedly without deterioration."))

story.append(Bold("Electrode Reactions:"))
story.append(F("Positive electrode: NiOOH + H2O + e⁻ ⇌ Ni(OH)2 + OH⁻"))
story.append(F("Negative electrode: MH_x + OH⁻ ⇌ MH_(x-1) + H2O + e⁻"))
story.append(P("Where M = metallic alloy (stores hydrogen); MH_x = metal hydride"))

story.append(Bold("Electrode Materials:"))
story.append(B("<b>Positive electrode:</b> Nickel oxide (NiOOH) — same as NiCd"))
story.append(B("<b>Negative electrode:</b> Metal hydride alloy (AB5 or AB2 type)"))
story.append(B("AB5 alloy: A = mixture of rare earth elements; B = partially substituted nickel"))
story.append(B("AB2 alloy: A = titanium or zirconium; B = partially substituted nickel — higher H2 storage, lower cost"))
story.append(B("<b>Electrolyte:</b> Aqueous KOH (alkaline) — same as NiCd"))

story.append(Bold("Key Performance Data:"))
story.append(B("Nominal cell voltage: ~1.2 V (same as NiCd)"))
story.append(B("Specific energy: 60–80 Wh/kg (significantly higher than NiCd)"))
story.append(B("Specific power: up to 250 W/kg"))
story.append(B("Flat discharge characteristics"))

story.append(Bold("Advantages over NiCd:"))
story.append(B("Higher capacity and specific energy (60–80 vs 30–50 Wh/kg)"))
story.append(B("No toxic cadmium — more environmentally friendly"))
story.append(B("Much longer cycle life than lead-acid"))
story.append(B("Safe and abuse tolerant"))

story.append(Bold("Disadvantages:"))
story.append(B("Relatively high cost"))
story.append(B("Higher self-discharge rate than NiCd"))
story.append(B("Poor charge acceptance at elevated temperatures"))
story.append(B("Low cell efficiency"))

story.append(Bold("EV Applications:"))
story.append(B("Toyota Prius (HEV) — NiMH pack by Panasonic EV Energy"))
story.append(B("Toyota RAV-EV (EV)"))
story.append(B("Chrysler Epic minivan — NiMH pack, range 150 km"))
story.append(Note("Exam Tip: NiMH = hydrogen stored in metal alloy. Same voltage as NiCd (1.2V) but higher specific energy. Used in Toyota Prius."))
story.append(Spacer(1,0.2*cm))

story.append(Q("12. Explain the construction and working of lithium-ion batteries."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("Lithium-ion batteries are the dominant technology in modern EVs. They use lithium intercalated carbon as the negative electrode and lithium metallic oxide as the positive electrode, with an organic electrolyte. The process of intercalation (lithium ions being absorbed into the crystal lattice) is fully reversible."))

story.append(Bold("Construction:"))
story.append(B("<b>Positive electrode:</b> Lithium cobalt oxide (LiCoO2) — most common; alternative: LiNiO2 or LiMn2O4"))
story.append(B("<b>Negative electrode:</b> Lithium intercalated carbon (LixC6) — graphite or coke; hosts Li up to LiC6 (x≤1)"))
story.append(B("<b>Electrolyte:</b> Organic liquid electrolyte — allows Li+ ion transport"))
story.append(B("<b>Separator:</b> Porous polymer membrane — allows Li+ ions, blocks electrons"))

story.append(Bold("Working Principle — Discharge:"))
story.append(P("During discharge, Li+ ions are de-intercalated (released) from the graphite negative electrode and travel through the electrolyte to the LiCoO2 positive electrode where they are intercalated."))
story.append(F("Negative electrode: Li_xC6 → 6C + xLi⁺ + xe⁻   (0 < x < 1)"))
story.append(F("Positive electrode: xLi⁺ + xe⁻ + Li_(1-x)CoO2 → LiCoO2"))

story.append(Bold("Charging:"))
story.append(P("Li+ ions move in the opposite direction — from LiCoO2 back to the graphite. The process is fully reversible."))

story.append(Bold("ASCII Diagram:"))
liion_diag = [
    "  DISCHARGE                              CHARGE",
    "  Li+ →→→→→→→→→→→→→→→→→→→→→→→→→→→→→  ←←←←←←← Li+",
    "  ┌─────────────────┬─────────────────┐",
    "  │  LiCoO2 (+)     │  Carbon(-) LixC6│",
    "  │   Li inserted   │  Li released    │",
    "  │   ← Li+ enters  │  Li+ → exits   │",
    "  └─────────────────┴─────────────────┘",
    "           Organic Electrolyte",
    "      e⁻ flow through external circuit",
    "      → = discharge direction",
]
story.append(MonoBlock(liion_diag))

story.append(Bold("Key Performance Data:"))
story.append(B("Nominal cell voltage: 3.6 V (= 3 NiMH cells)"))
story.append(B("Specific energy: 150–200 Wh/kg"))
story.append(B("High energy efficiency, good high-temperature performance"))
story.append(B("Low self-discharge rate"))

story.append(Bold("Advantages:"))
story.append(B("Highest practical specific energy among rechargeable batteries"))
story.append(B("High cell voltage (3.6 V) — fewer cells needed"))
story.append(B("Low self-discharge, recyclable, good efficiency"))
story.append(Note("Exam Tip: Key concept = intercalation. Li+ ions insert/extract from crystal lattice (not chemical reaction of electrode material itself). This makes it highly reversible and long-lasting."))
story.append(Spacer(1,0.2*cm))

story.append(Q("13. Explain the working principle and characteristics of lithium-polymer batteries."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("Lithium-polymer (Li-poly) batteries evolved from research on solid-state electrolytes in the 1970s. They use a solid polymer as the electrolyte instead of a liquid, making them 'solid-state' batteries."))

story.append(Bold("Working Principle:"))
story.append(B("Uses a solid polymer electrolyte (polyethylene oxide + electrolyte salt)"))
story.append(B("Most promising positive electrode: vanadium oxide V6O13 — can intercalate up to 8 Li atoms per molecule"))
story.append(F("Positive electrode reaction: Li_x + V6O13 + xe⁻ ⇌ Li_xV6O13  (0 < x < 8)"))
story.append(B("Negative electrode: lithium intercalated into carbon (similar to Li-ion)"))
story.append(B("Solid polymer conducts Li+ ions at temperatures above 60°C"))

story.append(Bold("Key Characteristics:"))
lipoly_table = make_table(
    [Paragraph("<b>Parameter</b>", S['body']), Paragraph("<b>Description</b>", S['body'])],
    [
        [P("Electrolyte"), P("Solid polymer (polyethylene oxide) — no flammable liquid")],
        [P("Specific energy"), P("Potential for highest among all batteries (~200+ Wh/kg)")],
        [P("Operating temperature"), P("80–120°C required for ionic conduction")],
        [P("Form factor"), P("Thin, flexible — any size or shape to fit EV chassis")],
        [P("Safety"), P("Solid electrolyte — safer in accidents (no liquid spill/fire risk)")],
        [P("Cycle/calendar life"), P("Good cycle and calendar life")],
    ],
    col_widths=[5*cm, 11.5*cm]
)
story.append(lipoly_table)

story.append(Bold("Advantages:"))
story.append(B("Highest potential specific energy and power"))
story.append(B("Solid polymer replaces flammable liquid electrolyte — greater safety"))
story.append(B("Flexible thin cell → battery of any size or shape → ideal for EV chassis integration"))
story.append(B("Lithium in ionic form (less reactive than metallic lithium)"))

story.append(Bold("Main Disadvantage:"))
story.append(B("<b>High operating temperature requirement: 80–120°C</b> — polymer only conducts ions above 60°C"))
story.append(B("Requires thermal management system — adds complexity and weight"))
story.append(Note("Exam Tip: Li-poly = solid electrolyte = safest Li battery. Main disadvantage = needs 80–120°C operation. Advantage = flexible shape + safety."))
story.append(Spacer(1,0.2*cm))

story.append(Q("14. Explain the working principle of zinc-air batteries."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("Zinc-air batteries use a gaseous positive electrode (oxygen from air) and a metallic zinc negative electrode. They are analogous to a fuel cell, with zinc acting as the 'fuel.'"))

story.append(Bold("Working Principle:"))
story.append(B("<b>Positive electrode (cathode):</b> Gaseous oxygen from air — reduced at the cathode"))
story.append(B("<b>Negative electrode (anode):</b> Metallic zinc (Zn) — oxidized (sacrificial electrode)"))
story.append(B("<b>Electrolyte:</b> Potassium hydroxide (KOH) — alkaline"))
story.append(B("During discharge: zinc is oxidized to zinc hydroxide [Zn(OH)2]"))
story.append(B("Air (oxygen) is continuously supplied to the cathode from the atmosphere"))

story.append(Bold("Recharging:"))
story.append(P("Zinc-air batteries are <b>mechanically rechargeable</b> — not electrically rechargeable. Recharging involves:"))
story.append(B("Replacing the discharged zinc hydroxide product with fresh zinc electrodes"))
story.append(B("Sending discharged electrode and KOH electrolyte to a recycling facility"))
story.append(B("Recharging time is rapid with suitable infrastructure"))

story.append(Bold("Key Performance Data:"))
story.append(B("Specific energy: ~200 Wh/kg (tested in Mercedes Benz postal vans)"))
story.append(B("Specific power: modest ~100 W/kg at 80% DoD"))
story.append(B("Range: 300–600 km between mechanical recharges"))

story.append(Bold("Positive electrode advantage:"))
story.append(P("Since the battery is recharged outside the vehicle, the positive electrode can be optimized purely for discharge characteristics. This is an attractive design feature."))
story.append(Note("Exam Tip: Zinc-air = mechanically rechargeable (not electrically). Zinc is like 'fuel' — analogous to a fuel cell. High specific energy (200 Wh/kg) but low specific power."))
story.append(Spacer(1,0.2*cm))

story.append(Q("15. Explain the construction and working of sodium-sulphur batteries."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("Sodium-sulphur (NaS) batteries use sodium as the negative electrode and sulphur as the positive electrode, with beta-alumina as the solid electrolyte. They operate at high temperatures (~300°C)."))

story.append(Bold("Construction:"))
story.append(B("<b>Negative electrode (anode):</b> Liquid sodium (Na) — high electrochemical reduction potential (2.71 V), low atomic mass (23.0)"))
story.append(B("<b>Positive electrode (cathode):</b> Liquid sulphur (S) — readily available, low cost"))
story.append(B("<b>Electrolyte:</b> Beta-alumina (sodium aluminium oxide) — solid ceramic, discovered by Ford Motor Co. in 1966"))
story.append(B("Both sodium and sulphur are in liquid state at operating temperature of ~300°C"))
story.append(B("Beta-alumina selectively conducts Na+ ions — separates liquid sodium from liquid sulphur"))

story.append(Bold("Working Principle:"))
story.append(P("During discharge, liquid sodium is oxidized at the anode and Na+ ions migrate through beta-alumina to react with sulphur at the cathode:"))
story.append(F("2Na + xS → Na2Sx   (sodium polysulphide)"))

story.append(Bold("Advantages:"))
story.append(B("High energy and power densities"))
story.append(B("Sodium is abundant and low cost; sulphur is also cheap"))
story.append(B("High specific energy: practical 150–300 Wh/kg"))

story.append(Bold("Limitations:"))
story.append(B("<b>High operating temperature: ~300°C</b> — requires thermal insulation and control system"))
story.append(B("Minimum size requirement limits development to large EVs only"))
story.append(B("Absence of overcharge mechanism — one cell going high resistance pulls down entire pack"))
story.append(B("<b>Safety concern:</b> Molten sodium + sulphur reaction can cause explosive heat release in accidents"))
story.append(B("Safety issues led to eventual discontinuation of NaS battery development programs"))
story.append(Note("Exam Tip: NaS = operates at 300°C with liquid sodium and sulphur. Main issues = safety (explosive in accidents) + high temperature requirement. Led to development of ZEBRA batteries."))
story.append(Spacer(1,0.2*cm))

story.append(Q("16. Explain the working principle of sodium-metal chloride (ZEBRA) batteries."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("The sodium-metal-chloride (ZEBRA) battery is a derivative of the NaS battery with built-in overcharge and overdischarge protection. ZEBRA = Zeolite Battery Research Africa Project (UK-South Africa collaboration, early 1980s)."))

story.append(Bold("Construction:"))
story.append(B("<b>Negative electrode:</b> Liquid sodium (same as NaS battery)"))
story.append(B("<b>Positive electrode:</b> Nickel chloride (NiCl2) or mixture of NiCl2 + ferrous chloride (FeCl2) — replaces sulphur in NaS"))
story.append(B("<b>Electrolyte:</b> Beta-alumina solid electrolyte (same as NaS)"))
story.append(B("<b>Secondary electrolyte:</b> Sodium chloraluminite (NaAlCl4) — introduced between NiCl2 and beta-alumina for good ionic contact"))

story.append(Bold("Cell Reactions:"))
story.append(F("NiCl2 + 2Na ⇌ Ni + 2NaCl  (2.58 V)"))
story.append(F("FeCl2 + 2Na ⇌ Fe + 2NaCl  (2.35 V)"))

story.append(Bold("Assembly:"))
story.append(P("Cells are assembled in discharged state using inexpensive raw materials:"))
story.append(B("Positive electrode prefabricated from Ni (or Fe) powder + NaCl (common salt)"))
story.append(B("On charging, pure sodium is manufactured in situ via diffusion through beta-alumina"))
story.append(B("This gives two advantages: in-situ Na production + cheap raw materials"))

story.append(Bold("Advantages over NaS:"))
story.append(B("Built-in overcharge and overdischarge protection"))
story.append(B("Uses cheaper raw materials (common salt + metal powder)"))
story.append(B("Safer than NaS (no liquid sulphur)"))
story.append(B("Wider operating temperature range with nickel as metallic component"))
story.append(B("Proven safe under all conditions of use"))

story.append(Bold("Limitation:"))
story.append(B("NaAlCl4 secondary electrolyte reduces specific energy by ~10%"))
story.append(B("Still requires high operating temperature (similar to NaS ~300°C)"))
story.append(B("High temperature management system required"))
story.append(Note("Exam Tip: ZEBRA = Na + NiCl2 + beta-alumina. Key advantage over NaS: overcharge/overdischarge protection + safety. Assembled in discharged state using common salt."))
story.append(Spacer(1,0.2*cm))

# ── SECTION: Fuel Cells ───────────────────────────────────────────────────────
story.append(SH("4. FUEL CELL BASIC PRINCIPLE AND OPERATION"))
story.append(HR())

story.append(Q("17. Explain the basic structure and working principle of a hydrogen fuel cell."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("A fuel cell is an electrochemical device that continuously produces electricity from the chemical reaction of hydrogen and oxygen, as long as fuel is supplied. It is the reverse of electrolysis of water."))

story.append(Bold("Basic Structure:"))
story.append(B("<b>Anode (negative):</b> Where hydrogen fuel is oxidized — releases protons (H+) and electrons"))
story.append(B("<b>Cathode (positive):</b> Where oxygen is reduced — combines with H+ and e- to form water"))
story.append(B("<b>Electrolyte:</b> Between anode and cathode — conducts H+ ions, blocks electrons"))
story.append(B("<b>Catalyst:</b> Platinum on both electrodes — speeds up electrochemical reactions"))
story.append(B("<b>External circuit:</b> Path for electron flow — this flow IS the electric current"))

story.append(Bold("Working Principle — Step by Step:"))
story.append(B("<b>Step 1:</b> Hydrogen (H2) is fed to the anode"))
story.append(B("<b>Step 2:</b> Catalyst at anode splits H2 into 2H+ (protons) + 2e-"))
story.append(B("<b>Step 3:</b> H+ ions travel through electrolyte from anode to cathode"))
story.append(B("<b>Step 4:</b> e- flow through external circuit → produce electrical current"))
story.append(B("<b>Step 5:</b> Oxygen is fed to cathode; O2 molecule breaks into O atoms"))
story.append(B("<b>Step 6:</b> Each O atom grabs 2e- from external circuit → becomes O²-"))
story.append(B("<b>Step 7:</b> O²- combines with 2H+ → H2O (water) — the only by-product"))

story.append(Bold("Chemical Reactions:"))
story.append(F("Anode:   H2 → 2H⁺ + 2e⁻"))
story.append(F("Cathode: 2e⁻ + 2H⁺ + 1/2(O2) → H2O"))
story.append(F("Cell:    H2 + 1/2(O2) → H2O + Electrical Energy"))

story.append(Bold("Fuel Cell Diagram:"))
fc_diag = [
    "  Hydrogen ─→ ┌───────┬───────────┬───────┐ ←─ Oxygen (air)",
    "              │ANODE  │ELECTROLYTE│CATHODE│",
    "  Unreacted ←─│(-)    │           │(+)    │─→ Water",
    "  Hydrogen    │       │  H⁺→→→→→  │       │",
    "              │H2→2H⁺ │           │H⁺+e-  │",
    "              │  +2e⁻ │           │ →H2O  │",
    "              └───────┴───────────┴───────┘",
    "                 e⁻ ────────────────→ e⁻",
    "                 External Circuit (Electric Current)",
    "                    ↓",
    "                 Load (Motor Drive / EV)",
]
story.append(MonoBlock(fc_diag))

story.append(Bold("Thermodynamics — Fuel Cell Efficiency:"))
story.append(P("Fuel cells operate isothermally — not subject to Carnot efficiency limit. Maximum electrical energy equals the Gibbs free energy change:"))
story.append(F("W_el = -ΔG = nFE"))
story.append(B("n = 2 electrons per H2 molecule"))
story.append(B("F = 96,485 C/mol (Faraday's constant)"))
story.append(B("E0 = 1.23 V (maximum reversible potential under standard conditions)"))
story.append(B("ΔG = -236 kJ/mol for H2 + 1/2O2 → H2O"))

story.append(Bold("Key Advantage:"))
story.append(B("Only by-product is water — zero harmful emissions at point of use"))
story.append(B("Higher theoretical efficiency than heat engines (not Carnot limited)"))
story.append(Note("Exam Tip: Draw the fuel cell structure with anode, cathode, electrolyte, external circuit. Write all three chemical reactions. Mention Gibbs free energy and the reversible potential E0 = 1.23 V."))
story.append(Spacer(1,0.2*cm))

story.append(Q("18. Explain the difference between batteries and fuel cells."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("Both batteries and fuel cells are electrochemical devices that convert chemical energy to electrical energy. However, they differ fundamentally in how they store and generate energy."))

story.append(Bold("Detailed Comparison Table:"))
bvfc = make_table(
    [Paragraph("<b>Aspect</b>", S['body']), Paragraph("<b>Battery</b>", S['body']),
     Paragraph("<b>Fuel Cell</b>", S['body'])],
    [
        [P("Energy storage"), P("Stores chemical energy internally in electrodes"), P("Generates electricity from externally supplied fuel")],
        [P("Energy limit"), P("Finite — limited by stored reactants"), P("Unlimited as long as fuel is supplied")],
        [P("Recharging"), P("Needs electrical recharging when depleted"), P("Needs refuelling (hydrogen supply)"), ],
        [P("Electrode role"), P("Consumed and regenerated during cycle"), P("Act as catalysts — not consumed")],
        [P("By-product"), P("Heat, sometimes gas (H2 in PbA)"), P("Water only (in H2-O2 cells)")],
        [P("Specific energy"), P("Limited (35–200 Wh/kg practical)"), P("Higher (fuel: 33,000 Wh/kg for H2)")],
        [P("Discharge profile"), P("Voltage drops as discharge progresses"), P("More stable voltage output")],
        [P("Efficiency"), P("70–90% round-trip"), P("40–65% electrical efficiency")],
        [P("EV suitability"), P("Currently dominant — Li-ion in all EVs"), P("Long-term potential — infrastructure lacking")],
    ],
    col_widths=[3.5*cm, 6.5*cm, 6.5*cm]
)
story.append(bvfc)

story.append(Bold("Summary:"))
story.append(B("Battery: closed system — stores and releases chemical energy from within"))
story.append(B("Fuel cell: open system — continuously converts externally supplied fuel to electricity"))
story.append(Note("Exam Tip: The KEY difference = battery stores energy (finite), fuel cell generates energy (infinite as long as fuel is supplied). This is the most important conceptual distinction."))
story.append(Spacer(1,0.2*cm))

# ── SECTION: Types of Fuel Cells ─────────────────────────────────────────────
story.append(SH("5. TYPES OF FUEL CELLS"))
story.append(HR())

story.append(Q("19. Mention the different types of fuel cells and explain them in brief."))
story.append(A())
story.append(Bold("Overview:"))
story.append(P("There are six major types of fuel cells, classified primarily by their electrolyte type and operating temperature."))

story.append(Bold("Comparison Table of All Fuel Cell Types:"))
fc_table = make_table(
    [Paragraph("<b>Type</b>", S['body']), Paragraph("<b>Electrolyte</b>", S['body']),
     Paragraph("<b>Temp</b>", S['body']), Paragraph("<b>Efficiency</b>", S['body']),
     Paragraph("<b>Application</b>", S['body'])],
    [
        [P("AFC (Alkaline)"), P("KOH solution"), P("~80°C"), P("40–60%"), P("Mobile/vehicle")],
        [P("PEMFC"), P("Nafion polymer"), P("~80°C"), P("40–50%"), P("EV & HEV (main)"), ],
        [P("DMFC"), P("Solid polymer"), P("90–120°C"), P("~30%"), P("Small EV devices")],
        [P("PAFC"), P("Phosphoric acid"), P("~200°C"), P("40–50%"), P("Stationary >250kW")],
        [P("MCFC"), P("Carbonate"), P("600–700°C"), P("50–60%"), P("Stationary >250kW")],
        [P("SOFC"), P("Yttria-zirconia"), P("~1000°C"), P("50–65%"), P("Stationary (best)")],
    ],
    col_widths=[2.8*cm, 3.5*cm, 2.2*cm, 2.5*cm, 5.5*cm]
)
story.append(fc_table)

story.append(Bold("1. Alkaline Fuel Cell (AFC):"))
story.append(B("Electrolyte: aqueous KOH solution"))
story.append(B("Operating temp: ~80°C — suitable for vehicles"))
story.append(B("Efficiency: up to 60%"))
story.append(B("Requires pure hydrogen (intolerant of CO2 contamination)"))
story.append(B("Used in NASA space shuttle, moon buggy"))

story.append(Bold("2. Proton Exchange Membrane Fuel Cell (PEMFC):"))
story.append(B("Electrolyte: Nafion solid polymer membrane"))
story.append(B("Operating temp: ~80°C — fast start-up, suitable for vehicles"))
story.append(B("Efficiency: ~40%"))
story.append(B("Highest power density among fuel cells (0.35–0.6 W/cm2)"))
story.append(B("Can tolerate impure hydrogen (unlike AFC)"))
story.append(B("<b>Best choice for EVs and HEVs</b>"))

story.append(Bold("3. Direct Methanol Fuel Cell (DMFC):"))
story.append(B("Fuel: methanol (reformed internally to hydrogen)"))
story.append(B("Same principle as PEM but at higher temperature (90–120°C)"))
story.append(B("Efficiency: ~30% (lowest) — still in research stage"))
story.append(B("Suitable for small portable devices and small EVs"))

story.append(Bold("4. Phosphoric Acid Fuel Cell (PAFC):"))
story.append(B("Electrolyte: phosphoric acid"))
story.append(B("Operating temp: ~200°C"))
story.append(B("Efficiency: ~40%; cogeneration possible"))
story.append(B("Too bulky for transportation — primarily stationary applications"))
story.append(B("Oldest type — traces back to original fuel cell concept"))

story.append(Bold("5. Molten Carbonate Fuel Cell (MCFC):"))
story.append(B("Electrolyte: molten carbonate salt"))
story.append(B("Operating temp: 600–700°C"))
story.append(B("Efficiency: 50–60%; excellent cogeneration"))
story.append(B("Can use coal gas, hydrogen, or methanol as fuel"))
story.append(B("Not suitable for vehicles — too hot and slow start-up"))

story.append(Bold("6. Solid Oxide Fuel Cell (SOFC):"))
story.append(B("Electrolyte: yttria-stabilized zirconia (solid ceramic)"))
story.append(B("Operating temp: ~1000°C (ITSOFC: lower temp version)"))
story.append(B("Efficiency: 50–65% (highest)"))
story.append(B("Best for stationary power generation; not suitable for vehicles"))
story.append(Note("Exam Tip: For EV = PEMFC (best) or AFC. For stationary = SOFC (highest efficiency). Know all 6 names, electrolytes, temperatures, and efficiency values."))
story.append(Spacer(1,0.2*cm))

# ── SECTION: PEMFC ────────────────────────────────────────────────────────────
story.append(SH("6. PEMFC AND ITS OPERATION"))
story.append(HR())

story.append(Q("20. Explain the construction and working principle of a proton exchange membrane fuel cell."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("The Proton Exchange Membrane Fuel Cell (PEMFC) is the most promising fuel cell for EV and HEV applications. First developed in 1960s for U.S. space program, it is now the leading technology for automotive fuel cells."))

story.append(Bold("Construction — Key Components:"))

story.append(Bold("1. Polymer Membrane (Electrolyte):"))
story.append(B("Material: Perfluorosulfonic acid polymer (Nafion by DuPont)"))
story.append(B("Acidic membrane — transports H+ (proton) ions from anode to cathode"))
story.append(B("Solid electrolyte — does not change, move, or vaporize"))
story.append(B("Must be kept humid — dry membrane blocks H+ conduction"))

story.append(Bold("2. Catalyst Layer:"))
story.append(B("Material: Platinum (Pt) — noble metal"))
story.append(B("Coated on carbon support and in direct contact with membrane and diffusion layer"))
story.append(B("Constitutes the electrode — extremely active for H2 oxidation and O2 reduction"))
story.append(B("Loading reduced from 28 mg to 0.2 mg/cm² through technology improvements"))
story.append(B("Cathode most critical: O2 reduction is harder than H2 oxidation"))

story.append(Bold("3. Gas Diffusion Layer:"))
story.append(B("Porous carbon paper or carbon cloth"))
story.append(B("Allows reactant gases (H2, O2) to reach catalyst layer"))
story.append(B("Also removes product water from cathode"))

story.append(Bold("4. Membrane-Electrode Assembly (MEA):"))
story.append(B("Assembly of: Membrane + Catalyst layers + Gas diffusion layers"))
story.append(B("This is the heart of the PEMFC"))

story.append(Bold("Working Principle:"))
story.append(F("Anode:   H2 → 2H⁺ + 2e⁻"))
story.append(F("Cathode: 1/2(O2) + 2H⁺ + 2e⁻ → H2O"))
story.append(F("Cell:    H2 + 1/2(O2) → H2O + Electrical Energy"))
story.append(B("H2 fed to anode → catalyst splits H2 → H+ ions and electrons"))
story.append(B("H+ ions cross through Nafion membrane → to cathode"))
story.append(B("Electrons flow through external circuit → produce electricity"))
story.append(B("At cathode: H+ + e- + O → H2O (water by-product)"))

story.append(Bold("PEMFC Structure Diagram:"))
pem_diag = [
    "  H2 →→  ┌──────────┬──────────┬──────────┐  ←← O2/Air",
              "          │ Gas Diff │          │ Gas Diff│",
    "  Unreacted│ Layer   │ Nafion   │  Layer   │→ Water",
    "  H2 ←←  │ ANODE   │Membrane  │ CATHODE  │",
    "          │ Catalyst│(electro- │ Catalyst │",
    "          │ (Pt)   │ lyte)    │  (Pt)    │",
    "          │ H2→2H+  │ H+→→→→→  │ H++e-→H2O│",
    "          │  +2e-   │          │          │",
    "          └──────────┴──────────┴──────────┘",
    "            e- ──────────────────────────→",
    "                  External Circuit → Load (EV Motor)",
]
story.append(MonoBlock(pem_diag))

story.append(Bold("Critical Issues in PEMFC:"))
story.append(Bold("Issue 1 — Water Management:"))
story.append(B("Membrane must be humid for H+ conduction"))
story.append(B("Too dry: insufficient ions → poor performance"))
story.append(B("Too wet (flooded): pores blocked → reactant gases cannot reach catalyst"))
story.append(B("Water forms at cathode and must be carefully managed"))

story.append(Bold("Issue 2 — Catalyst Poisoning:"))
story.append(B("Platinum catalyst is poisoned by carbon monoxide (CO) and sulfur"))
story.append(B("CO binds to Pt surface and blocks H2 from reacting"))
story.append(B("If H2 comes from a reformer, stream may contain CO"))
story.append(B("CO poisoning is reversible but costly to manage"))

story.append(Bold("Issue 3 — Catalyst Cost:"))
story.append(B("Platinum is expensive — major cost driver"))
story.append(B("R&D aims to reduce Pt loading and find alternative catalysts"))

story.append(Bold("Advantages for EV Applications:"))
story.append(B("Low temperature (~80°C) → fast start-up — essential for vehicles"))
story.append(B("Highest power density among all fuel cells (0.35–0.6 W/cm2)"))
story.append(B("Solid electrolyte — no corrosion risk, no liquid spillage"))
story.append(B("Only liquid is water — clean operation"))
story.append(B("Can tolerate fuel impurities (unlike AFC)"))

story.append(Bold("Disadvantages:"))
story.append(B("Expensive platinum catalyst and Nafion membrane"))
story.append(B("CO poisoning of catalyst"))
story.append(B("Complex water management system"))
story.append(B("Electrical efficiency only ~40% (lower than AFC)"))
story.append(Note("Exam Tip: PEMFC = Nafion + Pt catalyst + operates at 80°C. Three critical issues: water management, CO poisoning, cost. Best for EVs due to: low temp + high power density + solid electrolyte."))
story.append(Spacer(1,0.2*cm))

# ── SECTION: Supercapacitors ──────────────────────────────────────────────────
story.append(SH("7. SUPERCAPACITORS"))
story.append(HR())

story.append(Q("21. Explain the working principle of supercapacitors."))
story.append(A())
story.append(Bold("Introduction:"))
story.append(P("Supercapacitors (also called ultracapacitors or electrochemical double-layer capacitors) are energy storage devices that bridge the gap between conventional capacitors and batteries. They store energy electrostatically — without chemical reactions — allowing extremely fast charge/discharge and very long cycle life."))

story.append(Bold("Basic Capacitor Concept:"))
story.append(P("A conventional capacitor stores energy by separation of equal positive and negative electrostatic charges on two conducting plates separated by a dielectric insulator."))
story.append(F("Capacitance: C = q/V   [Farads]"))
story.append(B("C ∝ dielectric constant of insulating material"))
story.append(B("C ∝ 1/(distance between plates)"))
story.append(B("Conventional capacitors: very high power density (~10^12 W/m³) but very low energy density (~50 Wh/m³)"))

story.append(Bold("Supercapacitor Working Principle:"))
story.append(P("Supercapacitors store energy in TWO ways:"))
story.append(B("<b>1. Electrostatic double-layer charging:</b> Ions from electrolyte adsorb onto the surface of porous carbon electrodes, forming an electrical double layer — similar to a conventional capacitor but with much larger effective area"))
story.append(B("<b>2. Electrostatic charge in electrolyte:</b> Ions accumulate at electrode-electrolyte interface"))
story.append(P("No electrochemical (chemical) reactions occur inside a supercapacitor. Energy storage is purely physical/electrostatic."))

story.append(Bold("Ultracapacitor (Electrochemical) Principle:"))
story.append(P("Ultracapacitors use electrochemical systems to store energy in a polarized liquid layer at the interface between an ionically conducting electrolyte and an electrically conducting electrode. Faradaic (electrochemical) reactions are confined to surface layers and are fully reversible."))

story.append(Bold("Key Design Features:"))
story.append(B("<b>Porous carbon electrodes:</b> Extremely high internal surface area to maximize charge storage"))
story.append(B("<b>Ion transport:</b> Ions move much more slowly than electrons → longer time constant than conventional capacitors"))
story.append(B("<b>Electrolyte:</b> Ionic conductor filling the porous electrode structure"))

story.append(Bold("Supercapacitor Diagram:"))
sc_diag = [
    "  (+) Plate          Electrolyte (ions)       (-) Plate",
    "  ┌─────────┐       ┌─────────────────┐       ┌─────────┐",
    "  │ Porous  │       │  + + + + + + +  │       │ Porous  │",
    "  │ Carbon  │◄──────│  Positive ions  │──────►│ Carbon  │",
    "  │(high    │       │                 │       │(high    │",
    "  │ surface │       │  - - - - - - -  │       │ surface │",
    "  │ area)   │◄──────│  Negative ions  │──────►│  area)  │",
    "  └─────────┘       └─────────────────┘       └─────────┘",
    "  Double layer                                Double layer",
    "  of charge                                   of charge",
    "                    No chemical reaction!",
    "                    Pure electrostatic storage",
]
story.append(MonoBlock(sc_diag))

story.append(Bold("Performance Comparison:"))
sc_table = make_table(
    [Paragraph("<b>Parameter</b>", S['body']), Paragraph("<b>Conventional Capacitor</b>", S['body']),
     Paragraph("<b>Supercapacitor</b>", S['body']), Paragraph("<b>Battery</b>", S['body'])],
    [
        [P("Power density"), P("~10^12 W/m³"), P("~10^6 W/m³"), P("~10^4 W/m³")],
        [P("Energy density"), P("~50 Wh/m³"), P("~10^4 Wh/m³"), P("5–25 × 10^4 Wh/m³")],
        [P("Discharge time"), P("Microseconds"), P("~110 seconds"), P("~5000 seconds")],
        [P("Cycle life"), P("Very high"), P("~10^5 cycles"), P("100–1000 cycles")],
        [P("Mechanism"), P("Electrostatic"), P("Electrostatic + ionic"), P("Electrochemical")],
    ],
    col_widths=[4*cm, 4.5*cm, 4.5*cm, 3.5*cm]
)
story.append(sc_table)

story.append(Bold("Applications in EVs and HEVs:"))
story.append(B("<b>Regenerative braking:</b> Absorb large power pulses rapidly — far faster than batteries can charge"))
story.append(B("<b>Acceleration assist:</b> Release stored energy rapidly during sudden acceleration or hill climbing"))
story.append(B("<b>Buffer/intermediate storage:</b> Work alongside batteries or fuel cells to handle transient power demands"))
story.append(B("<b>Battery life extension:</b> By handling transient peaks, supercapacitors prevent stress on batteries → extends battery cycle life"))
story.append(B("Research target: 4000 W/kg and 15 Wh/kg for next-generation ultracapacitors"))

story.append(Bold("Why Supercapacitors Cannot Replace Batteries Entirely:"))
story.append(B("Energy density (~10^4 Wh/m³) is much lower than batteries (5–25×10^4 Wh/m³)"))
story.append(B("Cannot provide sustained energy for long trips"))
story.append(B("Best used in hybrid with batteries/fuel cells for complementary characteristics"))
story.append(Note("Exam Tip: Key principle = NO chemical reaction, pure electrostatic. Three advantages: (1) high power density, (2) fast charge/discharge, (3) very long cycle life (10^5). Used alongside batteries in EVs for transient demands."))

# ── QUICK REFERENCE ───────────────────────────────────────────────────────────
story.append(PageBreak())
story.append(section_banner("MODULE 4 — QUICK REFERENCE SUMMARY"))
story.append(Spacer(1, 0.3*cm))

story.append(SH("Key Formulas"))
form_data = [
    [Paragraph("<b>Parameter</b>", S['body']), Paragraph("<b>Formula</b>", S['body']),
     Paragraph("<b>Unit</b>", S['body'])],
    [P("Theoretical Capacity"), F("Q_T = x·n·F = 0.278·F·(m_R·n/M_m)"), P("Ah")],
    [P("Discharge Rate"), F("I = Q_T / t"), P("A")],
    [P("State of Charge"), F("SoC_T(t) = Q_T - ∫i(τ)dτ"), P("Ah")],
    [P("State of Discharge"), F("SoD_T(t) = ∫i(τ)dτ"), P("Ah")],
    [P("Depth of Discharge"), F("DoD(t) = (Q_T - SoC_T)/Q_T × 100%"), P("%")],
    [P("Peukert's Equation"), F("I^n × t_cut = λ"), P("—")],
    [P("Practical Capacity"), F("Q = λ / I^(n-1)"), P("Ah")],
    [P("Fractional Depletion"), F("DoD(t) = [∫(i^n/λ)dt] × 100%"), P("%")],
    [P("Fuel Cell Energy"), F("W_el = -ΔG = nFE"), P("J")],
    [P("Reversible Potential"), F("E0 = 1.23 V (H2-O2 at STP)"), P("V")],
    [P("Capacitance"), F("C = q/V"), P("F (Farad)")],
]
form_t = Table(form_data, colWidths=[5.5*cm, 7.5*cm, 3.5*cm], repeatRows=1)
form_t.setStyle(TableStyle([
    ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1a237e')),
    ('TEXTCOLOR', (0,0), (-1,0), colors.white),
    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
    ('FONTSIZE', (0,0), (-1,-1), 9),
    ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#e8eaf6'), colors.white]),
    ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#9fa8da')),
    ('TOPPADDING', (0,0), (-1,-1), 4),
    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ('LEFTPADDING', (0,0), (-1,-1), 5),
    ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
]))
story.append(form_t)

story.append(Spacer(1, 0.4*cm))
story.append(SH("Battery Types — Quick Comparison"))
batt_comp = make_table(
    [Paragraph("<b>Battery</b>", S['body']), Paragraph("<b>+Electrode</b>", S['body']),
     Paragraph("<b>-Electrode</b>", S['body']), Paragraph("<b>Electrolyte</b>", S['body']),
     Paragraph("<b>Sp. Energy</b>", S['body']), Paragraph("<b>Voltage</b>", S['body'])],
    [
        [P("Lead-Acid"), P("PbO2"), P("Pb"), P("H2SO4 (aq)"), P("35–50 Wh/kg"), P("2 V/cell")],
        [P("NiCd"), P("NiOOH"), P("Cd"), P("KOH (aq)"), P("30–50 Wh/kg"), P("1.2–1.3 V")],
        [P("NiMH"), P("NiOOH"), P("MHx alloy"), P("KOH (aq)"), P("60–80 Wh/kg"), P("~1.2 V")],
        [P("Li-ion"), P("LiCoO2"), P("LixC6 (graphite)"), P("Organic liquid"), P("150–200 Wh/kg"), P("3.6 V")],
        [P("Li-poly"), P("V6O13"), P("Li-carbon"), P("Solid polymer"), P("~200 Wh/kg"), P("~3 V")],
        [P("NaS"), P("Sulphur (S)"), P("Sodium (Na)"), P("Beta-alumina"), P("150–300 Wh/kg"), P("~2.1 V")],
        [P("ZEBRA"), P("NiCl2/FeCl2"), P("Sodium (Na)"), P("Beta-alumina"), P("~120 Wh/kg"), P("2.58/2.35 V")],
    ],
    col_widths=[2.5*cm, 3*cm, 3.5*cm, 3.5*cm, 3*cm, 2*cm]
)
story.append(batt_comp)

story.append(Spacer(1, 0.4*cm))
story.append(SH("Fuel Cell Types — Quick Comparison"))
fc_comp = make_table(
    [Paragraph("<b>Type</b>", S['body']), Paragraph("<b>Electrolyte</b>", S['body']),
     Paragraph("<b>Temp</b>", S['body']), Paragraph("<b>Efficiency</b>", S['body']),
     Paragraph("<b>Best For</b>", S['body'])],
    [
        [P("AFC"), P("KOH"), P("~80°C"), P("40–60%"), P("Mobile/Vehicle")],
        [P("PEMFC"), P("Nafion polymer"), P("~80°C"), P("40–50%"), P("EV/HEV (★ Best)")],
        [P("DMFC"), P("Solid polymer"), P("90–120°C"), P("~30%"), P("Small portable EVs")],
        [P("PAFC"), P("Phosphoric acid"), P("~200°C"), P("40–50%"), P("Stationary")],
        [P("MCFC"), P("Carbonate"), P("600–700°C"), P("50–60%"), P("Stationary")],
        [P("SOFC"), P("Zirconia ceramic"), P("~1000°C"), P("50–65%"), P("Stationary (★ Best)")],
    ],
    col_widths=[2.5*cm, 4*cm, 2.5*cm, 3*cm, 4.5*cm]
)
story.append(fc_comp)

story.append(Spacer(1, 0.4*cm))
story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#1a237e')))
story.append(Spacer(1, 0.2*cm))
story.append(Paragraph("— End of Module 4 Exam Notes —",
    ParagraphStyle('end', fontSize=11, fontName='Helvetica-Bold',
    textColor=colors.HexColor('#1a237e'), alignment=TA_CENTER)))
story.append(Spacer(1, 0.1*cm))
story.append(Paragraph("Covers all 2-Mark (Q1–Q40) and 8/10-Mark (Q1–Q21) questions from the Question Bank",
    ParagraphStyle('end2', fontSize=9, fontName='Helvetica',
    textColor=colors.HexColor('#455a64'), alignment=TA_CENTER)))

# ── Build PDF ─────────────────────────────────────────────────────────────────
output_path = "/mnt/user-data/outputs/Module_4_Exam_Notes.pdf"

def on_first_page(canvas, doc):
    canvas.saveState()
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(colors.HexColor('#9e9e9e'))
    canvas.drawRightString(WIDTH - 2*cm, 1.2*cm, "Module 4 — Energy Storage for EV and HEV")
    canvas.restoreState()

def on_later_pages(canvas, doc):
    canvas.saveState()
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(colors.HexColor('#9e9e9e'))
    canvas.drawString(2*cm, 1.2*cm, "Module 4 — Energy Storage for EV and HEV")
    canvas.drawRightString(WIDTH - 2*cm, 1.2*cm, f"Page {doc.page}")
    canvas.restoreState()

doc = SimpleDocTemplate(
    output_path, pagesize=A4,
    leftMargin=2*cm, rightMargin=2*cm,
    topMargin=2*cm, bottomMargin=2*cm,
    title="Module 4 - Energy Storage for EV and HEV - Exam Notes",
    author="Exam Prep"
)
doc.build(story, onFirstPage=on_first_page, onLaterPages=on_later_pages)
print(f"PDF created: {output_path}")
