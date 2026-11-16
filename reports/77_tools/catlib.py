"""Market category from slug + title (crypto up/down 5m/15m/4h, 1h/daily, crypto price, weather, sports, ...)."""
import re


def cat(s, title):
    t = (s + " " + title).lower()
    if re.search(r"-updown-(5m|15m|4h)-\d+$", s): return "crypto updown " + re.search(r"-updown-(5m|15m|4h)-", s).group(1)
    if "up-or-down" in s: return "crypto updown 1h/daily"
    if re.search(r"bitcoin|ethereum|solana|\bxrp\b|crypto|btc|dogecoin|bnb|hyperliquid", t): return "crypto price"
    if re.search(r"temperature|precipitation|rain|snow|hurricane|weather", t): return "weather"
    if re.search(r"\bvs\.?\b|nba|nfl|nhl|mlb|ncaa|premier|la liga|serie a|bundesliga|uefa|fifa|champions|tennis|atp|wta|ufc|fight|golf|f1|grand prix|cricket|esports|lol:|cs2|dota|valorant|match|game \d|spread|o/u|over/under|moneyline|win the 20|super bowl|world series", t): return "sports"
    if re.search(r"say|mention|tweet|post|truth social|x posts", t): return "mentions/posts"
    if re.search(r"cpi|inflation|fed |fomc|rate cut|unemployment|gdp|jobs|payroll|treasury|yield|s&p|nasdaq|dow|stock|earnings|close above|ipo", t): return "economics/finance"
    if re.search(r"election|president|senate|house|governor|minister|parliament|trump|congress|seats|vote|poll|approval", t): return "politics"
    if re.search(r"netflix|billboard|box office|movie|song|album|spotify|oscar|grammy|emmy|tv|show", t): return "culture"
    if re.search(r"openai|anthropic|gpt|gemini|ai |model|openrouter|apple|google|tesla|nvidia|meta|microsoft", t): return "tech"
    return "other"
