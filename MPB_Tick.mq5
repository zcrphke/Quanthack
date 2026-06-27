//+------------------------------------------------------------------+
//| MPB_Tick.mq5 v9 — 4-Strategy Netting Account                     |
//|                                                                  |
//| STRATEGIES:                                                      |
//|   1. FX Carry (AUDUSD/GBPUSD/USDCAD/USDJPY, MA-filtered)        |
//|   2. Metals Trend (XAUUSD/XAGUSD, ATR trailing stop on server)   |
//|   3. Vol Breakout — crypto (BTCUSD/ETHUSD/SOLUSD/XRPUSD)        |
//|      + reconsidered FX (EURUSD/USDCHF/EURGBP/EURCHF) — same      |
//|      mechanism as crypto, NOT a carry strategy (see server docs) |
//|                                                                  |
//| NETTING ACCOUNT: one net position per symbol, PositionSelect()   |
//| EA-side stops: per-instrument price stop + catastrophic equity   |
//| Metals: NO max-hold (ATR trailing stop on server, not time)      |
//| Vol breakout (crypto + FX additions): max-hold 2 days            |
//| FX carry: max-hold 7 days (long-running carry positions)         |
//+------------------------------------------------------------------+
#property copyright "Syphonix 2026"
#property version   "9.00"
#property strict
#include <Trade\Trade.mqh>

// All 14 traded symbols across 3 strategy groups
input string InpSymbols   = "AUDUSD,GBPUSD,USDCAD,USDJPY,BTCUSD,ETHUSD,SOLUSD,XRPUSD,XAUUSD,XAGUSD,EURUSD,USDCHF,EURGBP,EURCHF";
input ENUM_TIMEFRAMES InpTF = PERIOD_H1;
input string InpServerURL  = "http://127.0.0.1:8003/rebalance";
input int    InpBars       = 200;   // H1 bars for carry symbols (CARRY_MA=48 needs ~50)
input int    InpDailyBars  = 90;    // native D1 bars for metals/vol-breakout symbols
                                     // (metals needs 63: 48-day lookback+14 ATR+1;
                                     // vol breakout needs 12: 10-day lookback+2 — 90
                                     // gives comfortable margin for both, sent on D1
                                     // timeframe directly rather than resampled from H1)
input int    InpTimeoutMs  = 8000;
input int    InpCheckSec   = 60;
input double InpLocalDDPct = 5.0;   // local daily DD failsafe (%)
input double InpCatStopPct = 8.0;   // catastrophic per-position stop (% of equity)
input long   InpMagic      = 770099;

// Max hold by instrument class (hours)
// Carry: 168h (7 days — carry positions run for days/weeks)
// Vol breakout (crypto + FX additions): 48h (2-day hold, exits on schedule)
// Metals: 0    (no hold limit — ATR trailing stop exits, not time)
input double InpMaxHoldFX     = 168.0;
input double InpMaxHoldCrypto = 48.0;

CTrade   Trade;
string   Syms[];
int      NSym = 0;
datetime LastBarTime   = 0;
datetime LastCheckTime = 0;
datetime LastRankCheck = 0;
double   DayStartEquity = 0.0;
int      CurDay = -1;
bool     LocalHalt = false;

// -----------------------------------------------------------------------
// Per-instrument price stop (% of entry price, data-driven from last week)
// -----------------------------------------------------------------------
double StopPct(const string sym) {
  if(sym=="BTCUSD") return 6.00;
  if(sym=="ETHUSD") return 6.90;
  if(sym=="SOLUSD") return 10.38;
  if(sym=="XRPUSD") return 10.50;
  if(sym=="XAUUSD") return 6.51;
  if(sym=="XAGUSD") return 10.76;
  return 1.50;   // FX carry + FX vol-breakout additions (all majors/crosses, same calm vol)
}

// Is this a vol-breakout symbol (crypto OR reconsidered FX additions)?
// Both groups use the SAME mechanism and SAME 2-day hold — see server docs
// for why the FX additions are vol-breakout, not carry.
bool IsVolBreakout(const string sym) {
  return (sym=="BTCUSD"||sym=="ETHUSD"||sym=="SOLUSD"||sym=="XRPUSD"||
          sym=="EURUSD"||sym=="USDCHF"||sym=="EURGBP"||sym=="EURCHF");
}

// Max hold in hours per symbol (0 = no limit)
double MaxHoldHrs(const string sym) {
  if(IsVolBreakout(sym))
    return InpMaxHoldCrypto;   // 2-day hold for ALL vol-breakout symbols
  if(sym=="XAUUSD"||sym=="XAGUSD")
    return 0.0;   // metals: ATR stop on server side, no time limit here
  return InpMaxHoldFX;   // FX carry: 7 days
}

// -----------------------------------------------------------------------
int OnInit() {
  NSym = StringSplit(InpSymbols, ',', Syms);
  for(int i = 0; i < NSym; i++) {
    StringTrimLeft(Syms[i]); StringTrimRight(Syms[i]);
    if(!SymbolSelect(Syms[i], true))
      PrintFormat("WARN: cannot select %s", Syms[i]);
  }
  Trade.SetExpertMagicNumber(InpMagic);
  Trade.SetDeviationInPoints(50);
  DayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);
  CurDay = DayOfYear();
  EventSetTimer(30);
  PrintFormat("MPB v8 | %d symbols | FX-Carry + Metals-Trend + Crypto-VolBO | %s",
              NSym, StringSubstr(InpServerURL,0,40));
  return INIT_SUCCEEDED;
}

void OnDeinit(const int r) { EventKillTimer(); }
void OnTick()  { Check(); }
void OnTimer() { Check(); }

// -----------------------------------------------------------------------
void Check() {
  LocalFailsafe();
  if(LocalHalt) return;

  EnforceStops();   // price stops + catastrophic stop + time stops — every tick

  // Rank file check every 5 min
  datetime now = TimeCurrent();
  if((int)(now - LastRankCheck) >= 300) {
    ReadRankFile();
    LastRankCheck = now;
  }

  // Rebalance: on new H1 bar OR every InpCheckSec seconds
  datetime barTime = iTime(_Symbol, InpTF, 0);
  bool newBar  = (barTime != LastBarTime);
  bool timeDue = ((int)(now - LastCheckTime) >= InpCheckSec);
  if(!newBar && !timeDue) return;
  if(newBar) LastBarTime = barTime;
  LastCheckTime = now;

  string body = BuildJSON();
  if(body == "") { Print("[skip] empty body"); return; }
  string reply = HttpPost(InpServerURL, body);
  if(reply == "") return;
  ApplyReply(reply);
}

// -----------------------------------------------------------------------
// EnforceStops: per-instrument price stop + catastrophic equity stop + time stop
// NETTING: iterate Syms[] and use PositionSelect(sym) — one position per symbol
// -----------------------------------------------------------------------
void EnforceStops() {
  double equity = AccountInfoDouble(ACCOUNT_EQUITY);
  datetime now  = TimeCurrent();
  for(int i = 0; i < NSym; i++) {
    string sym = Syms[i];
    if(!PositionSelect(sym)) continue;
    double open    = PositionGetDouble(POSITION_PRICE_OPEN);
    double cur     = PositionGetDouble(POSITION_PRICE_CURRENT);
    long   ptype   = PositionGetInteger(POSITION_TYPE);
    double profit  = PositionGetDouble(POSITION_PROFIT);
    datetime opened= (datetime)PositionGetInteger(POSITION_TIME);

    // 1. Per-instrument price stop (adverse move %)
    double adverse = (ptype==POSITION_TYPE_BUY)
                     ? (open-cur)/open*100.0
                     : (cur-open)/open*100.0;
    if(adverse >= StopPct(sym)) {
      PrintFormat("PRICE-STOP %s: adverse=%.2f%% limit=%.2f%% — exit", sym, adverse, StopPct(sym));
      SetTargetPosition(sym, 0.0);
      continue;
    }

    // 2. Catastrophic equity stop (single position lost > InpCatStopPct % of equity)
    if(equity > 0 && profit < 0 && (-profit/equity*100.0) >= InpCatStopPct) {
      PrintFormat("CAT-STOP %s: position loss=%.2f%% equity — exit", sym, -profit/equity*100.0);
      SetTargetPosition(sym, 0.0);
      continue;
    }

    // 3. Time stop (0 = no limit for metals)
    double limitHrs = MaxHoldHrs(sym);
    if(limitHrs > 0) {
      double heldHrs = (double)(now - opened) / 3600.0;
      if(heldHrs >= limitHrs) {
        PrintFormat("TIME-STOP %s: held=%.1fh limit=%.0fh — exit", sym, heldHrs, limitHrs);
        SetTargetPosition(sym, 0.0);
        continue;
      }
    }
  }
}

// -----------------------------------------------------------------------
// ReadRankFile: EA reads rank.txt every 5 min; you update it from leaderboard
// -----------------------------------------------------------------------
int CurrentRank = 500;
void ReadRankFile() {
  int fh = FileOpen("rank.txt", FILE_READ|FILE_TXT);
  if(fh == INVALID_HANDLE) return;
  string line = FileReadString(fh);
  FileClose(fh);
  int r = (int)StringToInteger(line);
  if(r > 0 && r != CurrentRank) {
    CurrentRank = r;
    PrintFormat("rank updated: %d", CurrentRank);
  }
}

// -----------------------------------------------------------------------
// BuildJSON: sends equity + per-symbol net positions + H1 price bars
// Metals also sent as H1 data; server resamples to daily internally
// -----------------------------------------------------------------------
// Which symbols need NATIVE DAILY bars (metals + vol breakout), vs H1
// (carry symbols, whose 48h MA filter needs hourly resolution).
// Reuses the same grouping as IsVolBreakout() but also includes metals,
// since both metals-trend and vol-breakout strategies operate on daily
// bars server-side.
bool IsDailyBarSymbol(const string sym) {
  return IsVolBreakout(sym) || sym=="XAUUSD" || sym=="XAGUSD";
}

string BuildJSON() {
  double equity = AccountInfoDouble(ACCOUNT_EQUITY);
  double marginUsed   = AccountInfoDouble(ACCOUNT_MARGIN);       // margin currently committed
  double marginLevel  = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL); // equity/margin*100 -- competition stop-out metric (LOWER is worse, 30% = elimination)
  // marginUsedPct: margin / equity * 100 -- this is what the 90% risk-discipline
  // penalty threshold refers to (HIGHER is worse) -- different ratio than margin level,
  // do not confuse the two: marginLevel and marginUsedPct move in OPPOSITE directions.
  double marginUsedPct = (equity > 0) ? (marginUsed / equity * 100.0) : 0.0;

  // Gross position leverage: sum(|position value|) / equity. Computed from
  // actual open positions, not ACCOUNT_LEVERAGE (which is the broker's account
  // leverage setting, not current gross exposure -- a different number entirely).
  double grossExposure = 0.0;
  for(int i = 0; i < NSym; i++) {
    string sym = Syms[i];
    double net = OwnNet(sym);
    if(MathAbs(net) < 1e-9) continue;
    double price = SymbolInfoDouble(sym, SYMBOL_BID);
    double contractSize = SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE);
    if(price <= 0 || contractSize <= 0) continue;
    grossExposure += MathAbs(net) * contractSize * price;
  }
  double grossLeverage = (equity > 0) ? (grossExposure / equity) : 0.0;

  // Largest single-instrument share of gross exposure (competition's
  // >90%-single-instrument risk-discipline penalty check).
  double maxInstrumentValue = 0.0;
  for(int i = 0; i < NSym; i++) {
    string sym = Syms[i];
    double net = OwnNet(sym);
    if(MathAbs(net) < 1e-9) continue;
    double price = SymbolInfoDouble(sym, SYMBOL_BID);
    double contractSize = SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE);
    if(price <= 0 || contractSize <= 0) continue;
    double val = MathAbs(net) * contractSize * price;
    if(val > maxInstrumentValue) maxInstrumentValue = val;
  }
  double maxInstrumentPct = (grossExposure > 0) ? (maxInstrumentValue / grossExposure * 100.0) : 0.0;

  // Daily drawdown vs the EA's own daily-start equity tracker (same one
  // LocalFailsafe uses), so server-side reasoning matches local failsafe state.
  double dailyDdPct = (DayStartEquity > 0) ? MathMax(0.0, (DayStartEquity - equity) / DayStartEquity * 100.0) : 0.0;

  string js = "{\"equity\":" + DoubleToString(equity,2)
            + ",\"rank\":"          + IntegerToString(CurrentRank)
            + ",\"margin_used\":"   + DoubleToString(marginUsed,2)
            + ",\"margin_level\":"  + DoubleToString(marginLevel,2)
            + ",\"margin_used_pct\":" + DoubleToString(marginUsedPct,2)
            + ",\"gross_leverage\":" + DoubleToString(grossLeverage,3)
            + ",\"max_instrument_pct\":" + DoubleToString(maxInstrumentPct,2)
            + ",\"daily_dd_pct\":"   + DoubleToString(dailyDdPct,3)
            + ",\"prev_pos\":{";

  // NETTING: one position per symbol via PositionSelect()
  bool first = true;
  for(int i = 0; i < NSym; i++) {
    string sym = Syms[i];
    double net = OwnNet(sym);
    if(MathAbs(net) < 1e-9) continue;
    if(!first) js += ",";
    js += "\"" + sym + "\":" + DoubleToString(net, 4);
    first = false;
  }
  js += "},\"symbols\":{";

  int added = 0;
  for(int i = 0; i < NSym; i++) {
    string s = Syms[i];
    bool isDaily = IsDailyBarSymbol(s);
    ENUM_TIMEFRAMES tf = isDaily ? PERIOD_D1 : InpTF;
    int barsWanted      = isDaily ? InpDailyBars : InpBars;

    double c[]; ArraySetAsSeries(c, false);
    int got = CopyClose(s, tf, 1, barsWanted-1, c);
    if(got < 20) continue;
    double liveBid = SymbolInfoDouble(s, SYMBOL_BID);
    if(liveBid <= 0) liveBid = c[got-1];
    if(added > 0) js += ",";
    js += "\"" + s + "\":{\"contract\":"
       +  DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_CONTRACT_SIZE), 2)
       +  ",\"tf\":\"" + (isDaily ? "D1" : "H1") + "\""
       +  ",\"price\":"  + DoubleToString(liveBid, 5)
       +  ",\"close\":[";
    for(int k = 0; k < got; k++) { if(k > 0) js += ","; js += DoubleToString(c[k], 5); }
    js += "," + DoubleToString(liveBid, 5) + "]}";
    added++;
  }
  js += "}}";
  return (added > 0) ? js : "";
}

// -----------------------------------------------------------------------
string HttpPost(const string url, const string body) {
  char data[], result[];
  string headers = "Content-Type: application/json\r\n", rh;
  StringToCharArray(body, data, 0, StringLen(body));
  ResetLastError();
  int code = WebRequest("POST", url, headers, InpTimeoutMs, data, result, rh);
  if(code == -1) { PrintFormat("WebRequest err %d", GetLastError()); return ""; }
  if(code != 200) { PrintFormat("HTTP %d", code); return ""; }
  return CharArrayToString(result);
}

// -----------------------------------------------------------------------
void ApplyReply(const string reply) {
  string lines[];
  int n = StringSplit(reply, '\n', lines);
  if(n < 1) return;
  string h[]; if(StringSplit(lines[0], ',', h) < 4) return;
  if(h[3] == "1") { Print("SERVER HALT — flattening all"); FlattenAll(); return; }
  for(int i = 1; i < n; i++) {
    string f[]; if(StringSplit(lines[i], ',', f) < 2) continue;
    string sym = f[0]; StringTrimLeft(sym); StringTrimRight(sym);
    if(sym == "") continue;
    SetTargetPosition(sym, StringToDouble(f[1]));
  }
}

// -----------------------------------------------------------------------
// NETTING position management
// -----------------------------------------------------------------------

double OwnNet(const string sym) {
  if(!PositionSelect(sym)) return 0.0;
  double v = PositionGetDouble(POSITION_VOLUME);
  return (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? v : -v;
}

void SetTargetPosition(const string sym, double target) {
  if(!SymbolSelect(sym, true)) return;
  double cur   = OwnNet(sym);
  double step  = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
  double delta = target - cur;
  if(MathAbs(delta) < step * 0.5) return;
  Trade.SetTypeFillingBySymbol(sym);
  if(delta > 0) {
    double lots = NormLots(sym, delta);
    if(lots < step) return;
    if(!Trade.Buy(lots, sym))
      PrintFormat("BUY %s %.4f FAILED err=%d", sym, lots, GetLastError());
    else
      PrintFormat("BUY %s %.4f (net: %.4f→%.4f)", sym, lots, cur, target);
  } else {
    double lots = NormLots(sym, -delta);
    if(lots < step) return;
    if(!Trade.Sell(lots, sym))
      PrintFormat("SELL %s %.4f FAILED err=%d", sym, lots, GetLastError());
    else
      PrintFormat("SELL %s %.4f (net: %.4f→%.4f)", sym, lots, cur, target);
  }
}

double NormLots(const string sym, double v) {
  double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
  double vmin = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
  double vmax = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
  double m = MathFloor(v / step + 1e-9) * step;
  if(m < vmin) m = 0.0;
  if(vmax > 0 && m > vmax) m = vmax;
  return m;
}

void FlattenAll() {
  for(int i = 0; i < NSym; i++) {
    string sym = Syms[i];
    double net = OwnNet(sym);
    if(MathAbs(net) < SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP) * 0.5) continue;
    Trade.SetTypeFillingBySymbol(sym);
    if(net > 0) Trade.Sell(NormLots(sym,  net), sym);
    else        Trade.Buy (NormLots(sym, -net), sym);
  }
}

// -----------------------------------------------------------------------
void LocalFailsafe() {
  int d = DayOfYear();
  double eq = AccountInfoDouble(ACCOUNT_EQUITY);
  if(d != CurDay) { CurDay = d; DayStartEquity = eq; LocalHalt = false; }
  double dd = (DayStartEquity - eq) / DayStartEquity * 100.0;
  if(!LocalHalt && dd >= InpLocalDDPct) {
    PrintFormat("LOCAL DD FAILSAFE: %.2f%% >= %.2f%% — flattening all", dd, InpLocalDDPct);
    FlattenAll();
    LocalHalt = true;
  }
}

int DayOfYear() {
  MqlDateTime st; TimeToStruct(TimeCurrent(), st); return st.day_of_year;
}
