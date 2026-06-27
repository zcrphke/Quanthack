//+------------------------------------------------------------------+
//| MPB_BTC.mq5 v1 — BTC-Only Multi-Timeframe Strategy              |
//|                                                                  |
//| Sends M5, H1, H3 bars for BTCUSD to the Python server.          |
//| All other instruments are sent as flat (0) targets.             |
//| Sharpe measured every 15 min by competition.                    |
//+------------------------------------------------------------------+
#property copyright "Syphonix 2026"
#property version   "1.00"
#property strict
#include <Trade\Trade.mqh>

input string InpServerURL  = "http://127.0.0.1:8003/rebalance";
input int    InpM5Bars     = 100;   // M5 bars to send (covers MOMENTUM_LB*3 = 36 bars min)
input int    InpH1Bars     = 50;    // H1 bars
input int    InpH3Bars     = 30;    // H3 bars (PERIOD_H3 or 3*H1)
input int    InpTimeoutMs  = 8000;
input int    InpCheckSec   = 60;    // check every 60s (server decides if 15-min window matters)
input double InpLocalDDPct = 2.0;   // flatten if daily DD exceeds this %
input double InpCatStopPct = 0.05;  // catastrophic stop: 0.05% of equity (~$500)
input long   InpMagic      = 880099;

CTrade   Trade;
datetime LastCheckTime = 0;
double   DayStartEquity = 0.0;
int      CurDay = -1;
bool     LocalHalt = false;

int OnInit() {
  if(!SymbolSelect("BTCUSD", true)) { Print("Cannot select BTCUSD"); return INIT_FAILED; }
  Trade.SetExpertMagicNumber(InpMagic);
  Trade.SetDeviationInPoints(200);  // BTC needs wider deviation
  DayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);
  CurDay = DayOfYear();
  EventSetTimer(30);
  PrintFormat("MPB_BTC v1 | BTC-Only MTF | M5(%d)+H1(%d)+H3(%d) | %s",
              InpM5Bars, InpH1Bars, InpH3Bars, InpServerURL);
  return INIT_SUCCEEDED;
}

void OnDeinit(const int r) { EventKillTimer(); }
void OnTick()  { Check(); }
void OnTimer() { Check(); }

void Check() {
  LocalFailsafe();
  if(LocalHalt) return;
  EnforceStop();
  datetime now = TimeCurrent();
  if((int)(now - LastCheckTime) < InpCheckSec) return;
  LastCheckTime = now;
  string body = BuildJSON();
  if(body == "") return;
  string reply = HttpPost(InpServerURL, body);
  if(reply == "") return;
  ApplyReply(reply);
}

// -----------------------------------------------------------------------
void EnforceStop() {
  if(!PositionSelect("BTCUSD")) return;
  double equity = AccountInfoDouble(ACCOUNT_EQUITY);
  double profit  = PositionGetDouble(POSITION_PROFIT);
  // Catastrophic stop: position losing > InpCatStopPct% of equity
  if(equity > 0 && profit < 0 && (-profit/equity*100.0) >= InpCatStopPct) {
    PrintFormat("CAT-STOP BTCUSD: loss=%.4f%% equity — flattening", -profit/equity*100.0);
    SetTargetPosition(0.0);
  }
}

// -----------------------------------------------------------------------
string BuildJSON() {
  double equity = AccountInfoDouble(ACCOUNT_EQUITY);
  double net = OwnNet("BTCUSD");
  double price = SymbolInfoDouble("BTCUSD", SYMBOL_BID);
  double contract = SymbolInfoDouble("BTCUSD", SYMBOL_TRADE_CONTRACT_SIZE);

  // Fetch M5, H1, H3 bars
  double m5[], h1[], h3[];
  ArraySetAsSeries(m5, false); ArraySetAsSeries(h1, false); ArraySetAsSeries(h3, false);
  int gm5 = CopyClose("BTCUSD", PERIOD_M5,  1, InpM5Bars-1, m5);
  int gh1 = CopyClose("BTCUSD", PERIOD_H1,  1, InpH1Bars-1, h1);
  int gh3 = CopyClose("BTCUSD", PERIOD_H3,  1, InpH3Bars-1, h3);

  if(gm5 < 10 || gh1 < 5 || price <= 0) {
    PrintFormat("BTC: insufficient data m5=%d h1=%d h3=%d", gm5, gh1, gh3);
    return "";
  }

  // Account metrics
  double marginLevel  = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
  double marginUsedPct = AccountInfoDouble(ACCOUNT_MARGIN) /
                         (equity > 0 ? equity : 1) * 100.0;
  double dailyDdPct = (DayStartEquity > 0) ?
                      MathMax(0, (DayStartEquity-equity)/DayStartEquity*100) : 0;

  string js = "{\"equity\":" + DoubleToString(equity, 2)
            + ",\"margin_level\":"   + DoubleToString(marginLevel, 2)
            + ",\"margin_used_pct\":" + DoubleToString(marginUsedPct, 3)
            + ",\"daily_dd_pct\":"   + DoubleToString(dailyDdPct, 4)
            + ",\"prev_pos\":{\"BTCUSD\":" + DoubleToString(net, 4) + "}"
            + ",\"symbols\":{\"BTCUSD\":{"
            + "\"contract\":" + DoubleToString(contract, 2)
            + ",\"price\":"   + DoubleToString(price, 2)
            + ",\"m5\":[";

  // M5 array
  for(int i=0; i<gm5; i++) { if(i>0) js+=","; js+=DoubleToString(m5[i],2); }
  js += "]";

  // H1 array
  js += ",\"h1\":[";
  for(int i=0; i<gh1; i++) { if(i>0) js+=","; js+=DoubleToString(h1[i],2); }
  js += "]";

  // H3 array
  js += ",\"h3\":[";
  for(int i=0; i<gh3; i++) { if(i>0) js+=","; js+=DoubleToString(h3[i],2); }
  js += "]";

  js += "}}}";
  return js;
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
  string h[];
  if(StringSplit(lines[0], ',', h) < 4) return;
  if(h[3] == "1") { Print("SERVER HALT — flattening BTC"); SetTargetPosition(0.0); return; }
  for(int i = 1; i < n; i++) {
    string f[];
    if(StringSplit(lines[i], ',', f) < 2) continue;
    string sym = f[0]; StringTrimLeft(sym); StringTrimRight(sym);
    if(sym == "BTCUSD") {
      SetTargetPosition(StringToDouble(f[1]));
      break;  // only care about BTC
    }
  }
}

// -----------------------------------------------------------------------
double OwnNet(const string sym) {
  if(!PositionSelect(sym)) return 0.0;
  double v = PositionGetDouble(POSITION_VOLUME);
  return (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? v : -v;
}

void SetTargetPosition(double target) {
  if(!SymbolSelect("BTCUSD", true)) return;
  double cur   = OwnNet("BTCUSD");
  double step  = SymbolInfoDouble("BTCUSD", SYMBOL_VOLUME_STEP);
  double delta = target - cur;
  if(MathAbs(delta) < step * 0.5) return;
  Trade.SetTypeFillingBySymbol("BTCUSD");
  if(delta > 0) {
    double lots = NormLots(delta);
    if(lots < step) return;
    if(!Trade.Buy(lots, "BTCUSD"))
      PrintFormat("BUY BTCUSD %.4f FAILED err=%d", lots, GetLastError());
    else
      PrintFormat("BUY BTCUSD %.4f (net: %.4f->%.4f)", lots, cur, target);
  } else {
    double lots = NormLots(-delta);
    if(lots < step) return;
    if(!Trade.Sell(lots, "BTCUSD"))
      PrintFormat("SELL BTCUSD %.4f FAILED err=%d", lots, GetLastError());
    else
      PrintFormat("SELL BTCUSD %.4f (net: %.4f->%.4f)", lots, cur, target);
  }
}

double NormLots(double v) {
  double step = SymbolInfoDouble("BTCUSD", SYMBOL_VOLUME_STEP);
  double vmin = SymbolInfoDouble("BTCUSD", SYMBOL_VOLUME_MIN);
  double vmax = SymbolInfoDouble("BTCUSD", SYMBOL_VOLUME_MAX);
  double m = MathFloor(v/step + 1e-9)*step;
  if(m < vmin) m = 0.0;
  if(vmax > 0 && m > vmax) m = vmax;
  return m;
}

void LocalFailsafe() {
  int d = DayOfYear();
  double eq = AccountInfoDouble(ACCOUNT_EQUITY);
  if(d != CurDay) { CurDay=d; DayStartEquity=eq; LocalHalt=false; }
  double dd = (DayStartEquity > 0) ? (DayStartEquity-eq)/DayStartEquity*100 : 0;
  if(!LocalHalt && dd >= InpLocalDDPct) {
    PrintFormat("LOCAL DD FAILSAFE: %.4f%% >= %.1f%% — flattening BTC", dd, InpLocalDDPct);
    SetTargetPosition(0.0);
    LocalHalt = true;
  }
}

int DayOfYear() {
  MqlDateTime st; TimeToStruct(TimeCurrent(), st); return st.day_of_year;
}
