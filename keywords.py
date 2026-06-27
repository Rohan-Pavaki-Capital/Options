"""
Keyword patterns for the Stage 1 lenient candidate filter.

PHILOSOPHY:
  Stage 1 is a CHEAP wide-net filter. Its job is to eliminate obvious
  non-candidates (TOC, blank pages, unrelated notes) without missing
  any legitimate options disclosure. Precision is the LLM's job in Stage 2.

A page becomes a candidate if ANY of these conditions match:
  1. NOTE_TITLE_PATTERNS match (strongest signal - actual section header)
  2. PLAN_CODE_KEYWORDS match (specific plan acronyms - rarely false positive)
  3. VALUATION_KEYWORDS match (Black-Scholes/Monte Carlo - very specific)
  4. 3+ ROLLFORWARD_PATTERNS matches (full opening→granted→exercised→closing sequence)
  5. GENERIC_KEYWORDS match AND page has a numeric table
  6. SECTION_HEADERS match AND page has a numeric table
  7. NON_ENGLISH_KEYWORDS match (German, French, Spanish, Chinese, Japanese)

All patterns are regex strings, applied case-insensitively against page text
(except non-ASCII patterns which are applied to the raw text).
"""

# ─────────────────────────────────────────────────────────────────────────
# TIER 1: HIGH-PRECISION PLAN CODES
# These acronyms rarely appear outside of share-based payment disclosures.
# A single match makes the page a candidate.
# ─────────────────────────────────────────────────────────────────────────
PLAN_CODE_KEYWORDS = [
    # UK plans
    r"\bLTIP\b",          # Long-Term Incentive Plan
    r"\bPSP\b",           # Performance Share Plan
    r"\bSAYE\b",          # Save As You Earn
    r"\bCSOP\b",          # Company Share Option Plan
    r"\bSIP\b",           # Share Incentive Plan
    r"\bSRSOS\b",         # Savings Related Share Option Scheme
    r"\bDBP\b",           # Deferred Bonus Plan
    r"\bDSP\b",           # Deferred Share Plan
    r"\bRSP\b",           # Restricted Share Plan
    r"\bESOS\b",          # Executive Share Option Scheme
    r"\bSOS\b",           # Share Option Scheme
    r"\bAESOP\b",         # All Employee Share Ownership Plan

    # US plans
    r"\bRSUs?\b",         # Restricted Stock Units
    r"\bPSUs?\b",         # Performance Stock Units
    r"\bESPP\b",          # Employee Stock Purchase Plan
    r"\bESOPs?\b",        # Employee Stock Ownership Plan
    r"\bNQSO\b",          # Non-Qualified Stock Options
    r"\bISO\b",           # Incentive Stock Options
    r"\bSARs?\b",         # Stock Appreciation Rights

    # Phrased plans
    r"save\s*as\s*you\s*earn",
    r"sharesave\s*(?:plan|scheme)",
    r"performance\s*share\s*plan",
    r"long[\s\-]*term\s*incentive\s*plan",
    r"restricted\s*stock\s*units?",
    r"restricted\s*share\s*units?",
    r"employee\s*stock\s*purchase",
    r"employee\s*share\s*ownership\s*plan",
    r"deferred\s*bonus\s*plan",
    r"founders?\s*performance\s*plan",
    r"co[\s\-]*investment\s*plan",

    # German plans (matched against lowercased text — see match_page)
    r"\baktienoptionsplan\b",
    r"\baktienoptionsprogramm\b",
    r"\bmitarbeiterbeteiligung(?:sprogramm|splan)?\b",
    r"\bmitarbeiteraktienprogramm\b",
    r"\bbelegschaftsaktien\b",
    r"\bphantomaktien\b",
    r"\bvirtuelle[rs]?\s*aktien\b",
    r"\bvirtuelle[rs]?\s*optionen\b",
    r"\bwertsteigerungsrechte\b",            # SARs
    r"\bbezugsrechte\b",
    r"\boptionsrechte\b",
    r"\baktienzusagen\b",
    r"\bperformance[\s\-]*aktien\b",
    r"\bmatching[\s\-]*aktien\b",
    r"\bwandelschuldverschreibung(?:en)?\b",
    r"\b(?:aop|vsop|mep)\b",                 # German plan abbreviations

    # Japanese plans (matched against text; str.lower() is a no-op for CJK,
    # so these work in the lowercased-text tiers). The three most common terms
    # — 新株予約権 / ストックオプション / 株式報酬 — are already in
    # NON_ENGLISH_KEYWORDS; below are the specific instruments NOT covered there.
    r"ストック\s*[・･]\s*オプション",          # stock option (middle-dot spelling)
    r"譲渡制限付株式",                          # restricted stock (incl. 〜報酬; RSU-equivalent)
    r"譲渡制限付新株予約権",                    # restricted stock acquisition rights
    r"株式給付信託",                            # share-grant trust (BIP / J-ESOP)
    r"株式交付信託",                            # share-delivery trust
    r"信託型ストック\s*[・･]?\s*オプション",    # trust-type stock option
    r"パフォーマンス\s*[・･]?\s*シェア",        # performance share
    r"リストリクテッド\s*[・･]?\s*ストック",    # restricted stock (katakana)

    # Korean plans (matched against text; str.lower() is a no-op for Hangul).
    # 주식매수선택권 / 성과주식 are already in NON_ENGLISH_KEYWORDS.
    r"양도제한조건부주식",                      # restricted stock (RSU-equivalent)
    r"스톡옵션",                                # stock option (loanword)
    r"주식선택권",                              # stock option (alt term)
    r"우리사주",                                # employee stock ownership (ESOP)

    # Portuguese plans (Brazil; matched against lowercased text).
    r"plano\s*de\s*op(?:ç|c)(?:ã|a)o\s*de\s*compra\s*de\s*a(?:ç|c)(?:õ|o)es",   # stock option plan
    r"plano\s*de\s*op(?:ç|c)(?:õ|o)es",                                          # option plan
    r"plano\s*de\s*incentivo\s*de\s*longo\s*prazo",                              # LTIP
    r"plano\s*de\s*a(?:ç|c)(?:õ|o)es\s*restritas",                               # restricted share plan
    r"a(?:ç|c)(?:õ|o)es\s*restritas",                                            # restricted shares
    r"unidades?\s*de\s*a(?:ç|c)(?:õ|o)es\s*restritas",                           # RSU
    r"a(?:ç|c)(?:õ|o)es\s*de\s*desempenho",                                      # performance shares
    r"outorga\s*de\s*op(?:ç|c)(?:õ|o)es",                                        # grant of options

    # Taiwanese plans (Traditional Chinese; str.lower() is a no-op for CJK).
    # 股權激勵 / 限制性股票 / 員工持股 are already in NON_ENGLISH_KEYWORDS.
    r"員工認股權(?:憑證)?",                     # employee stock options (/ warrant cert.)
    r"認股權憑證",                              # stock subscription warrant
    r"限制員工權利新股",                        # restricted stock to employees (Taiwan RSU)
    r"員工新股認購權",                          # employee new-share subscription right

    # Indonesian plans (Bahasa; matched against lowercased text).
    r"\b(?:msop|mesop)\b",                      # (Management) Employee Stock Option Program
    r"program\s*opsi\s*saham",                  # stock option program
    r"program\s*kepemilikan\s*saham",           # share ownership program (ESOP/EMSOP)
    r"opsi\s*saham\s*karyawan",                 # employee stock options

    # Thai plans (SEC 56-1 One Report; Thai script has no case, matched raw).
    r"\bejip\b",                                # Employee Joint Investment Program
    r"โครงการ\s*esop",                          # ESOP scheme
    r"ใบสำคัญแสดงสิทธิ(?:ที่จะซื้อหุ้น)?\s*esop",  # ESOP warrant
    r"โครงการร่วมลงทุนระหว่างนายจ้างและลูกจ้าง",   # EJIP (employer-employee co-investment)
    r"โครงการเสนอขายหลักทรัพย์แก่พนักงาน",        # employee securities offering scheme
]

# ─────────────────────────────────────────────────────────────────────────
# TIER 2: VALUATION MODEL KEYWORDS
# These appear almost exclusively in option valuation disclosures.
# A single match makes the page a candidate.
# ─────────────────────────────────────────────────────────────────────────
VALUATION_KEYWORDS = [
    r"black[\s\-]*scholes\s*(?:model|formula|valuation)?",
    r"monte[\s\-]*carlo\s*(?:simulation|model|valuation)",
    r"binomial\s*(?:model|valuation|tree|lattice)",
    r"trinomial\s*(?:model|tree|lattice)",
    r"option\s*pricing\s*model",
    r"lattice\s*model",
    r"finnerty\s*model",
    r"ghaidarov\s*(?:adjustment|model)",

    # German valuation models
    r"black[\s\-]*scholes[\s\-]*merton",
    r"monte[\s\-]*carlo[\s\-]*(?:simulation|modell)",
    r"binomial(?:modell|baum)",
    r"trinomial(?:modell|baum)",
    r"optionspreismodell",
    r"bewertungsmodell",

    # Japanese valuation models / terms
    r"ブラック\s*[・･]?\s*ショールズ",          # Black-Scholes
    r"モンテ\s*[・･]?\s*カルロ",                # Monte Carlo
    r"二項モデル",                              # binomial model
    r"格子モデル",                              # lattice model
    r"オプション(?:価格算定|評価)モデル",      # option pricing / valuation model
    r"公正な評価単価",                          # fair value per unit (always in SO notes)

    # Korean valuation models
    r"블랙\s*[-·]?\s*숄즈",                      # Black-Scholes
    r"몬테\s*카를로",                            # Monte Carlo
    r"이항모형",                                # binomial model
    r"옵션가격결정모형",                        # option pricing model

    # Portuguese valuation models (Brazil; lowercased text)
    r"modelo\s*binomial",                       # binomial model
    r"simula(?:ç|c)(?:ã|a)o\s*de\s*monte[\s\-]*carlo",   # Monte Carlo simulation
    r"modelo\s*de\s*precifica(?:ç|c)(?:ã|a)o\s*de\s*op(?:ç|c)(?:õ|o)es",  # option pricing model

    # Taiwanese valuation models (Traditional Chinese)
    r"布萊克\s*[-‧·]?\s*休斯(?:模型|模式)?",    # Black-Scholes
    r"蒙地卡羅",                                # Monte Carlo
    r"二項式(?:評價)?模式",                     # binomial model
    r"選擇權評價模式",                          # option pricing/valuation model
]

# ─────────────────────────────────────────────────────────────────────────
# TIER 3: GENERIC KEYWORDS (require corroboration)
# These can appear in policy text, so we only treat them as a signal
# if the page ALSO has a numeric table.
# ─────────────────────────────────────────────────────────────────────────
GENERIC_KEYWORDS = [
    r"share[\s\-]*based\s*(?:payment|compensation|remuneration)",
    r"share\s*options?",
    r"stock\s*options?",
    r"share\s*option\s*scheme",
    r"equity[\s\-]*settled\s*(?:share|award|option)",
    r"cash[\s\-]*settled\s*(?:share|award|option)",
    r"nil[\s\-]*cost\s*option",
    r"option\s*(?:plan|scheme|programme)",
    r"warrant\s*(?:plan|scheme|programme|holders?)",

    # German generic terms (require a numeric table, like their English peers)
    r"anteilsbasierte[rn]?\s*verg(?:ü|ue)tung",     # IFRS 2 German term
    r"aktienbasierte[rn]?\s*verg(?:ü|ue)tung",
    r"aktienbasierte[rn]?\s*verg(?:ü|ue)tungs(?:vereinbarung|programm|system)",
    r"aktienkursbasierte[rn]?\s*verg(?:ü|ue)tung",
    r"aktienoption(?:en)?",
    r"durch\s*eigenkapitalinstrumente\s*erf(?:ü|ue)llte",   # equity-settled
    r"in\s*bar\s*erf(?:ü|ue)llte",                          # cash-settled
    r"barausgleich",
    r"optionsprogramm",
    r"optionsplan",

    # Japanese generic terms (require a numeric table, like their English/German peers)
    r"株式に基づく報酬",                        # share-based payment (IFRS term)
    r"株式報酬費用",                            # share-based compensation expense
    r"株式報酬制度",                            # share-based compensation scheme

    # Korean generic terms (require a numeric table, like their peers)
    r"주식결제형",                              # equity-settled
    r"현금결제형",                              # cash-settled
    r"주식보상비용",                            # share-based compensation expense

    # Portuguese generic terms (Brazil; require a numeric table, lowercased text)
    r"pagamento[s]?\s*baseado[s]?\s*em\s*a(?:ç|c)(?:õ|o)es",     # IFRS 2 / CPC 10 term
    r"remunera(?:ç|c)(?:ã|a)o\s*baseada\s*em\s*a(?:ç|c)(?:õ|o)es",
    r"op(?:ç|c)(?:õ|o)es\s*de\s*(?:compra\s*de\s*)?a(?:ç|c)(?:õ|o)es",   # share options
    r"liquidad[oa]s?\s*(?:com|em)\s*a(?:ç|c)(?:õ|o)es",          # equity-settled
    r"liquidad[oa]s?\s*em\s*caixa",                              # cash-settled

    # Taiwanese generic terms (require a numeric table; CJK)
    r"股份基礎給付",                            # share-based payment (IFRS 2, Taiwan term)
    r"權益交割",                                # equity-settled
    r"現金交割",                                # cash-settled
    r"認股權",                                  # stock options / subscription rights

    # Indonesian generic terms (Bahasa; require a numeric table, lowercased text)
    r"pembayaran\s*berbasis\s*saham",           # share-based payment (PSAK 53 / IFRS 2)
    r"kompensasi\s*berbasis\s*saham",           # share-based compensation
    r"opsi\s*saham",                            # share / stock options
    r"diselesaikan\s*dengan\s*(?:instrumen\s*)?(?:ekuitas|saham)",   # equity-settled
    r"diselesaikan\s*dengan\s*kas",             # cash-settled

    # Thai generic terms (SEC 56-1 One Report; Thai script, matched raw)
    r"การจ่ายโดยใช้หุ้นเป็นเกณฑ์",                # share-based payment (TFRS 2 term)
    r"การจ่ายโดยใช้หุ้นเป็นเกณ(?:ฑ์)?",            # share-based payment (spelling variant)
    r"ใบสำคัญแสดงสิทธิที่จะซื้อหุ้น",              # warrant to purchase shares
    r"สิทธิซื้อหุ้น",                            # right to buy shares (stock option)
    r"ราคาใช้สิทธิ",                            # exercise price
]

# ─────────────────────────────────────────────────────────────────────────
# TIER 4: ROLL-FORWARD TABLE PATTERNS
# These specifically describe the structure of an options activity table.
# 3+ matches alone (without other signals) makes a page a candidate.
# 1-2 matches require a plan keyword to also be present.
# ─────────────────────────────────────────────────────────────────────────
ROLLFORWARD_PATTERNS = [
    # Opening balance variants
    r"(?:outstanding|options?|awards?|units?|warrants?)\s*(?:at|as\s*at)\s*(?:the\s*)?(?:beginning|start)\s*of\s*(?:the\s*)?(?:year|period)",
    r"(?:outstanding|options?|awards?|units?|warrants?)\s*(?:at|as\s*at)\s*1\s*(?:january|february|march|april|may|june|july|august|september|october|november|december)",
    r"(?:opening|brought\s*forward)\s*balance",

    # Closing balance variants
    r"(?:outstanding|options?|awards?|units?|warrants?)\s*(?:at|as\s*at)\s*(?:the\s*)?(?:end|year[\s\-]*end)\s*of\s*(?:the\s*)?(?:year|period)",
    r"(?:outstanding|options?|awards?|units?|warrants?)\s*(?:at|as\s*at)\s*3[01]\s*(?:january|february|march|april|may|june|july|august|september|october|november|december)",
    r"(?:closing|carried\s*forward)\s*balance",

    # Activity verbs in table format
    r"granted\s*(?:during|in)\s*(?:the\s*)?(?:year|period)",
    r"(?:lapsed|forfeited|cancelled|expired|surrendered)\s*(?:during|in)\s*(?:the\s*)?(?:year|period)",
    r"exercised\s*(?:during|in)\s*(?:the\s*)?(?:year|period)",
    r"vested\s*(?:during|in)\s*(?:the\s*)?(?:year|period)",
    r"settled\s*(?:during|in)\s*(?:the\s*)?(?:year|period)",

    # Exercisable
    r"exercisable\s*(?:at|as\s*at)\s*(?:the\s*)?(?:beginning|end|year[\s\-]*end)",
    r"vested\s*and\s*exercisable",

    # ── German roll-forward vocabulary ──
    # Opening balance
    r"(?:bestand|stand|ausstehend)\s*(?:am\s*anfang|zu\s*beginn|zum\s*beginn)\s*des\s*gesch(?:ä|ae)ftsjahr",
    r"(?:bestand|stand)\s*zum\s*1\.?\s*januar",
    r"\banfangsbestand\b",
    # Activity
    r"im\s*gesch(?:ä|ae)ftsjahr\s*gew(?:ä|ae)hrt",
    r"\bgew(?:ä|ae)hrt\b",                 # granted
    r"\bausge(?:ü|ue)bt\b",                # exercised
    r"\bverfallen\b",                      # lapsed
    r"\bverwirkt\b",                       # forfeited
    r"verfallen\s*(?:oder|/|und)?\s*verwirkt",
    r"\bausgelaufen\b",                    # expired
    r"\bannulliert\b",                     # cancelled
    r"unverfallbar\s*geworden",            # vested
    r"\bausgeglichen\b",                   # settled
    # Closing balance
    r"(?:bestand|stand|ausstehend)\s*(?:am\s*ende|zum\s*ende)\s*des\s*gesch(?:ä|ae)ftsjahr",
    r"(?:bestand|stand)\s*zum\s*31\.?\s*dezember",
    r"\bendbestand\b",
    # Exercisable
    r"aus(?:ü|ue)bbar\s*(?:am\s*ende|zum\s*ende)",
    r"\baus(?:ü|ue)bbar\b",

    # ── Japanese roll-forward vocabulary ──
    # An options activity table (新株予約権の変動状況) carries several of these,
    # so the "3+ matches" rule fires on a real table while a stray policy
    # sentence (1-2 matches) only qualifies alongside a plan keyword.
    r"権利確定",                # vested
    r"権利行使",                # exercised
    r"行使可能",                # exercisable
    r"失効",                    # lapsed / forfeited
    r"付与数?",                 # granted (付与 / 付与数)
    r"未行使残高",              # outstanding (unexercised) balance

    # ── Korean roll-forward vocabulary ──
    # 주식기준보상 activity tables carry several of these; the "3+ matches"
    # rule fires on a real table while a stray sentence needs a plan keyword too.
    r"부여",                    # granted
    r"권리행사",                # exercised
    r"행사가능",                # exercisable
    r"소멸",                    # lapsed / forfeited
    r"가득",                    # vested
    r"미행사",                  # unexercised (outstanding)

    # ── Portuguese roll-forward vocabulary (Brazil; lowercased text) ──
    r"saldo\s*(?:no|em)\s*in(?:í|i)cio\s*do\s*(?:exerc(?:í|i)cio|per(?:í|i)odo)",  # opening
    r"saldo\s*(?:no|em)\s*(?:fim|final)\s*do\s*(?:exerc(?:í|i)cio|per(?:í|i)odo)", # closing
    r"\boutorgad[oa]s\b",       # granted
    r"\bexercid[oa]s\b",        # exercised
    r"\bexerc(?:í|i)veis\b",    # exercisable
    r"\bcancelad[oa]s\b",       # cancelled
    r"\bextint[oa]s\b",         # extinguished / lapsed
    r"\bexpirad[oa]s\b",        # expired

    # ── Taiwanese roll-forward vocabulary (Traditional Chinese) ──
    # 股份基礎給付 activity tables carry several of these.
    r"期初",                    # beginning of period (opening balance)
    r"期末",                    # end of period (closing balance)
    r"本期(?:授予|給予)",       # granted during the period
    r"已(?:行使|執行)",         # exercised
    r"可(?:行使|執行)",         # exercisable
    r"失效",                    # lapsed / forfeited (Traditional)
    r"未行使",                  # unexercised (outstanding)

    # ── Hebrew roll-forward vocabulary (Israel; raw text) ──
    # A תשלום מבוסס מניות activity table carries several of these.
    r"הוענקו",                  # granted
    r"מומשו",                   # exercised
    r"פקעו",                    # expired / lapsed
    r"חולטו",                   # forfeited
    r"הבשילו",                  # vested
    r"יתרה\s*לתחילת\s*(?:ה)?שנה",   # balance at beginning of year (opening)
    r"יתרה\s*ל(?:סוף|תום)\s*(?:ה)?שנה",  # balance at end of year (closing)
    r"ניתנות\s*למימוש",         # exercisable
]

# ─────────────────────────────────────────────────────────────────────────
# TIER 5: SECTION HEADER PATTERNS (require numeric table)
# Generic section headings that need a numeric table to qualify.
# ─────────────────────────────────────────────────────────────────────────
SECTION_HEADERS = [
    r"share[\s\-]*based\s*payment",
    r"share[\s\-]*based\s*compensation",
    r"equity[\s\-]*based\s*compensation",
    r"stock[\s\-]*based\s*compensation",
    r"employee\s*share\s*(?:plans?|schemes?)",
    r"equity\s*(?:plans?|instruments?|compensation|incentive)",
    r"share\s*incentive\s*(?:plans?|schemes?)",

    # German section headers (require a numeric table)
    r"anteilsbasierte[rn]?\s*verg(?:ü|ue)tung",
    r"aktienbasierte[rn]?\s*verg(?:ü|ue)tung",
    r"aktienbasierte\s*verg(?:ü|ue)tungsprogramme",
    r"aktienoptionspl(?:ä|ae)ne",
    r"mitarbeiterbeteiligung",

    # Portuguese section headers (Brazil; require a numeric table)
    r"pagamento[s]?\s*baseado[s]?\s*em\s*a(?:ç|c)(?:õ|o)es",
    r"remunera(?:ç|c)(?:ã|a)o\s*baseada\s*em\s*a(?:ç|c)(?:õ|o)es",

    # Taiwanese section header (require a numeric table)
    r"股份基礎給付",
]

# ─────────────────────────────────────────────────────────────────────────
# TIER 6: NOTE TITLE PATTERNS (strongest signal)
# Matches actual note headers like "23. Share-based payments"
# A single match qualifies the page.
# ─────────────────────────────────────────────────────────────────────────
NOTE_TITLE_PATTERNS = [
    r"(?:^|\n)\s*(?:note\s*)?\d+[\.\:\)\s]+share[\s\-]*based\s*(?:payment|compensation|remuneration)",
    r"(?:^|\n)\s*(?:note\s*)?\d+[\.\:\)\s]+(?:employee\s*)?(?:share|stock)\s*(?:option|plan|scheme|award)",
    r"(?:^|\n)\s*(?:note\s*)?\d+[\.\:\)\s]+(?:share|equity|stock)[\s\-]*(?:based|incentive|compensation)",
    r"(?:^|\n)\s*(?:note\s*)?\d+[\.\:\)\s]+share[\s\-]*based\s*payment\s*arrangement",
    r"(?:^|\n)\s*(?:note\s*)?\d+[\.\:\)\s]+(?:long[\s\-]*term\s*)?incentive\s*(?:plan|award|scheme)",
    r"(?:^|\n)\s*(?:note\s*)?\d+[\.\:\)\s]+restricted\s*(?:share|stock)\s*(?:plan|award|unit)",

    # German note titles, e.g. "(34) Anteilsbasierte Vergütung", "Nr. 34 ...",
    # "Ziffer 34 ...". A single match qualifies the page.
    r"(?:^|\n)\s*(?:\(|nr\.?\s*|ziffer\s*|erl(?:ä|ae)uterung\s*)?\d+[\.\)\:\s]+(?:anteils|aktien)basierte[rn]?\s*verg(?:ü|ue)tung",
    r"(?:^|\n)\s*(?:\(|nr\.?\s*)?\d+[\.\)\:\s]+aktienoptions(?:plan|programm|pl(?:ä|ae)ne)",
    r"(?:^|\n)\s*(?:\(|nr\.?\s*)?\d+[\.\)\:\s]+mitarbeiterbeteiligung",

    # Japanese note headings. 「ストック・オプション等関係」 is the canonical
    # JGAAP 有価証券報告書 note title; a single match qualifies the page.
    r"ストック\s*[・･]?\s*オプション等関係",
    r"ストック\s*[・･]?\s*オプション等に関する事項",

    # Korean note heading. 「주식기준보상」 is the canonical K-IFRS note title;
    # a single match qualifies the page.
    r"주식기준보상",

    # Portuguese note titles (Brazil), e.g. "23. Pagamento baseado em ações"
    # or a bare CPC 10 / IFRS 2 note heading. A single match qualifies the page.
    r"(?:^|\n)\s*(?:nota\s*)?\d+[\.\)\:\s]+pagamento[s]?\s*baseado[s]?\s*em\s*a(?:ç|c)(?:õ|o)es",
    r"(?:^|\n)\s*(?:nota\s*)?\d+[\.\)\:\s]+remunera(?:ç|c)(?:ã|a)o\s*baseada\s*em\s*a(?:ç|c)(?:õ|o)es",

    # Taiwanese note heading. 「股份基礎給付」 is the canonical TW-IFRS note
    # title; a single match qualifies the page.
    r"股份基礎給付",

    # Hebrew note heading. תשלום מבוסס מניות is the canonical IFRS 2 (Hebrew)
    # note title; a single match qualifies the page.
    r"תשלום\s*מבוסס\s*מניות",
]

# ─────────────────────────────────────────────────────────────────────────
# TIER 7: NON-ENGLISH KEYWORDS
# International support — match against raw text (case-sensitive for some).
# ─────────────────────────────────────────────────────────────────────────
NON_ENGLISH_KEYWORDS = [
    # German keywords now live in the main tier lists above (PLAN_CODE,
    # GENERIC, ROLLFORWARD, SECTION_HEADERS, NOTE_TITLE). They are matched
    # against lowercased text, so capitalized German nouns + umlauts match
    # correctly — unlike this list, which matches non-ASCII patterns against
    # raw (case-sensitive) text.

    # French
    r"options?\s*(?:de|sur)\s*actions?",
    r"actions?\s*gratuites?",
    r"actions?\s*de\s*performance",
    r"r[ée]mun[ée]ration\s*(?:en|fond[ée]e\s*sur)\s*actions?",
    r"plan\s*d['']?attribution\s*d['']?actions?",
    r"plan\s*d['']?options?",

    # Spanish
    r"opciones\s*sobre\s*acciones",
    r"acciones\s*restringidas",
    r"plan\s*de\s*incentivos\s*a\s*largo\s*plazo",
    r"retribuci[óo]n\s*(?:en|basada\s*en)\s*acciones",

    # Italian
    r"piani\s*di\s*stock\s*option",
    r"compensi\s*basati\s*su\s*azioni",

    # Portuguese (Brazil)
    r"pagamento[s]?\s*baseado[s]?\s*em\s*a(?:ç|c)(?:õ|o)es",
    r"op(?:ç|c)(?:õ|o)es\s*de\s*(?:compra\s*de\s*)?a(?:ç|c)(?:õ|o)es",
    r"a(?:ç|c)(?:õ|o)es\s*restritas",
    r"plano\s*de\s*op(?:ç|c)(?:õ|o)es",

    # Chinese (Simplified & Traditional)
    r"股票期权",
    r"股權激勵",
    r"股权激励",
    r"限制性股票",
    r"限制性股權",
    r"員工持股",
    r"员工持股",
    r"股份支付",              # share-based payment (mainland CAS 11 note title — canonical)
    r"权益结算",              # equity-settled (mainland)
    r"现金结算",              # cash-settled (mainland)
    r"股份基礎給付",          # share-based payment (Taiwan IFRS 2 term)
    r"員工認股權",            # employee stock options (Taiwan)
    r"認股權憑證",            # stock subscription warrant (Taiwan)

    # Japanese
    r"ストック\s*オプション",
    r"株式報酬",
    r"新株予約権",

    # Korean
    r"주식매수선택권",
    r"성과주식",

    # Hebrew (Israel — TASE/MAYA; matched against raw text, no case folding).
    # תשלום מבוסס מניות is the canonical IFRS 2 (Hebrew) note title.
    r"תשלום\s*מבוסס\s*מניות",          # share-based payment (IFRS 2 Hebrew term)
    r"תגמול\s*מבוסס\s*מניות",          # share-based compensation
    r"כתבי\s*אופציה",                  # option warrants / certificates
    r"אופציות\s*לעובדים",              # employee options
    r"מניות\s*חסומות",                 # restricted shares
    r"יחידות\s*מניה\s*חסומות",         # restricted stock units (RSU)
    r"תכנית\s*אופציות",                # option plan
    r"תוכנית\s*אופציות",               # option plan (alt spelling)

    # Dutch (Netherlands — ESEF; large issuers e.g. ASML, Philips, Heineken)
    r"op\s*aandelen\s*gebaseerde\s*betalingen",      # IFRS 2 Dutch term
    r"op\s*aandelen\s*gebaseerde\s*belon(?:ing|ingen)",
    r"aandelenoptie(?:s|regeling|plan)?",
    r"aandelengebaseerde\s*betalingen",
    r"personeelsopties",
    r"prestatieaandelen",                            # performance shares

    # Swedish (Sweden — ESEF; large issuers e.g. Ericsson, Volvo, Atlas Copco)
    r"aktierelaterad[e]?\s*ers(?:ä|a)ttning(?:ar)?",  # IFRS 2 Swedish term
    r"aktiebaserad[e]?\s*ers(?:ä|a)ttning(?:ar)?",
    r"personaloptioner",
    r"teckningsoptioner",
    r"optionsprogram",
    r"aktiesparprogram",
    r"prestationsaktier",                            # performance shares

    # Thai (Thailand — SEC 56-1 One Report; Thai script, no case folding)
    r"การจ่ายโดยใช้หุ้นเป็นเกณฑ์",                   # share-based payment (TFRS 2 note title)
    r"ใบสำคัญแสดงสิทธิที่จะซื้อหุ้น",                 # warrant to purchase shares (ESOP-W)
    r"โครงการร่วมลงทุนระหว่างนายจ้างและลูกจ้าง",      # EJIP
]