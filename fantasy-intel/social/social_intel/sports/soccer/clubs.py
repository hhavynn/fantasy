"""Clubs the soccer adapter recognises: the top five European leagues and MLS.

The list is a superset of any one season, so promotion and relegation never lose a club people still
talk about. ``aliases`` always match. ``context_aliases`` are also everyday words or other sports' team
names ("Wolves", "Nice", "Union"): they need a soccer context and a capital letter. ``subreddit`` is set
only for communities confirmed to exist on Arctic Shift.
"""

from __future__ import annotations

from typing import NamedTuple


class Club(NamedTuple):
    id: str
    name: str
    league: str
    aliases: tuple[str, ...] = ()
    context_aliases: tuple[str, ...] = ()
    subreddit: str = ""


PL, LL, BL, SA, L1, MLS = "Premier League", "La Liga", "Bundesliga", "Serie A", "Ligue 1", "MLS"

CLUBS: tuple[Club, ...] = (
    Club("arsenal", "Arsenal", PL, ("arsenal", "the gunners", "gunners", "afc"), subreddit="Gunners"),
    Club("aston-villa", "Aston Villa", PL, ("aston villa", "avfc"), ("villa",), "avfc"),
    Club("bournemouth", "Bournemouth", PL, ("bournemouth", "the cherries")),
    Club("brentford", "Brentford", PL, ("brentford",), ("the bees",)),
    Club("brighton", "Brighton", PL, ("brighton", "brighton and hove albion"), ("seagulls",)),
    Club("burnley", "Burnley", PL, ("burnley", "the clarets")),
    Club("chelsea", "Chelsea", PL, ("chelsea", "cfc", "the blues"), subreddit="chelseafc"),
    Club("coventry", "Coventry City", PL, ("coventry", "coventry city")),
    Club("crystal-palace", "Crystal Palace", PL, ("crystal palace",), ("palace",)),
    Club("everton", "Everton", PL, ("everton", "the toffees"), subreddit="Everton"),
    Club("fulham", "Fulham", PL, ("fulham",)),
    Club("hull", "Hull City", PL, ("hull city",), ("hull",)),
    Club("ipswich", "Ipswich Town", PL, ("ipswich", "ipswich town")),
    Club("leeds", "Leeds United", PL, ("leeds", "leeds united")),
    Club("leicester", "Leicester City", PL, ("leicester", "leicester city", "the foxes")),
    Club("liverpool", "Liverpool", PL, ("liverpool", "lfc", "the reds"), subreddit="LiverpoolFC"),
    Club("man-city", "Manchester City", PL, ("manchester city", "man city", "mancity", "mcfc"), subreddit="MCFC"),
    Club("man-utd", "Manchester United", PL, ("manchester united", "man united", "man utd", "manutd", "mufc",
                                              "red devils"), subreddit="reddevils"),
    Club("newcastle", "Newcastle United", PL, ("newcastle", "newcastle united", "nufc", "the magpies"), subreddit="NUFC"),
    Club("nottingham-forest", "Nottingham Forest", PL, ("nottingham forest", "nottm forest", "nffc"), ("forest",)),
    Club("southampton", "Southampton", PL, ("southampton",), ("saints",)),
    Club("sunderland", "Sunderland", PL, ("sunderland", "the black cats")),
    Club("tottenham", "Tottenham Hotspur", PL, ("tottenham", "tottenham hotspur", "thfc", "coys"), ("spurs",), "coys"),
    Club("west-ham", "West Ham United", PL, ("west ham", "west ham united", "the hammers", "whufc"), subreddit="Hammers"),
    Club("wolves", "Wolverhampton Wanderers", PL, ("wolverhampton", "wolverhampton wanderers"), ("wolves",)),

    Club("alaves", "Alaves", LL, ("alaves",)),
    Club("athletic-bilbao", "Athletic Club", LL, ("athletic bilbao", "athletic club")),
    Club("atletico", "Atletico Madrid", LL, ("atletico madrid", "atletico", "atleti"), subreddit="atletico"),
    Club("barcelona", "Barcelona", LL, ("barcelona", "barca", "fc barcelona", "blaugrana"), subreddit="Barca"),
    Club("betis", "Real Betis", LL, ("real betis", "betis")),
    Club("celta", "Celta Vigo", LL, ("celta vigo", "celta")),
    Club("elche", "Elche", LL, ("elche",)),
    Club("espanyol", "Espanyol", LL, ("espanyol",)),
    Club("getafe", "Getafe", LL, ("getafe",)),
    Club("girona", "Girona", LL, ("girona",)),
    Club("levante", "Levante", LL, ("levante",)),
    Club("mallorca", "Mallorca", LL, ("mallorca",)),
    Club("osasuna", "Osasuna", LL, ("osasuna",)),
    Club("oviedo", "Real Oviedo", LL, ("real oviedo", "oviedo")),
    Club("rayo", "Rayo Vallecano", LL, ("rayo vallecano", "rayo")),
    Club("real-madrid", "Real Madrid", LL, ("real madrid", "los blancos", "madridistas"), ("madrid",), "realmadrid"),
    Club("real-sociedad", "Real Sociedad", LL, ("real sociedad", "la real")),
    Club("sevilla", "Sevilla", LL, ("sevilla",)),
    Club("valencia", "Valencia", LL, ("valencia cf",), ("valencia",)),
    Club("villarreal", "Villarreal", LL, ("villarreal", "yellow submarine")),

    Club("augsburg", "Augsburg", BL, ("augsburg",)),
    Club("bayern", "Bayern Munich", BL, ("bayern munich", "bayern", "fc bayern", "bayern munchen"), subreddit="FCBayern"),
    Club("dortmund", "Borussia Dortmund", BL, ("borussia dortmund", "dortmund", "bvb"), subreddit="borussiadortmund"),
    Club("leverkusen", "Bayer Leverkusen", BL, ("bayer leverkusen", "leverkusen")),
    Club("frankfurt", "Eintracht Frankfurt", BL, ("eintracht frankfurt", "frankfurt")),
    Club("freiburg", "Freiburg", BL, ("freiburg",)),
    Club("gladbach", "Borussia Monchengladbach", BL, ("borussia monchengladbach", "monchengladbach", "gladbach")),
    Club("hamburg", "Hamburger SV", BL, ("hamburger sv", "hamburg", "hsv")),
    Club("heidenheim", "Heidenheim", BL, ("heidenheim",)),
    Club("hoffenheim", "Hoffenheim", BL, ("hoffenheim",)),
    Club("koln", "FC Koln", BL, ("fc koln", "cologne", "koln")),
    Club("leipzig", "RB Leipzig", BL, ("rb leipzig", "leipzig")),
    Club("mainz", "Mainz", BL, ("mainz",)),
    Club("st-pauli", "St. Pauli", BL, ("st pauli",)),
    Club("stuttgart", "Stuttgart", BL, ("vfb stuttgart", "stuttgart")),
    Club("union-berlin", "Union Berlin", BL, ("union berlin",)),
    Club("bremen", "Werder Bremen", BL, ("werder bremen", "bremen")),
    Club("wolfsburg", "Wolfsburg", BL, ("wolfsburg",)),

    Club("atalanta", "Atalanta", SA, ("atalanta",)),
    Club("bologna", "Bologna", SA, ("bologna fc",), ("bologna",)),
    Club("cagliari", "Cagliari", SA, ("cagliari",)),
    Club("como", "Como", SA, ("como 1907",), ("como",)),
    Club("cremonese", "Cremonese", SA, ("cremonese",)),
    Club("fiorentina", "Fiorentina", SA, ("fiorentina",)),
    Club("genoa", "Genoa", SA, ("genoa cfc",), ("genoa",)),
    Club("inter", "Inter Milan", SA, ("inter milan", "internazionale", "inter milano", "nerazzurri"), ("inter",),
         "FCInterMilan"),
    Club("juventus", "Juventus", SA, ("juventus", "juve", "bianconeri"), subreddit="Juve"),
    Club("lazio", "Lazio", SA, ("lazio",)),
    Club("lecce", "Lecce", SA, ("lecce",)),
    Club("milan", "AC Milan", SA, ("ac milan", "rossoneri"), ("milan",), "ACMilan"),
    Club("napoli", "Napoli", SA, ("napoli", "ssc napoli"), subreddit="sscnapoli"),
    Club("parma", "Parma", SA, ("parma calcio",), ("parma",)),
    Club("pisa", "Pisa", SA, ("pisa sc",), ("pisa",)),
    Club("roma", "Roma", SA, ("as roma", "giallorossi"), ("roma",), "ASRoma"),
    Club("sassuolo", "Sassuolo", SA, ("sassuolo",)),
    Club("torino", "Torino", SA, ("torino fc", "torino")),
    Club("udinese", "Udinese", SA, ("udinese",)),
    Club("verona", "Hellas Verona", SA, ("hellas verona",), ("verona",)),

    Club("angers", "Angers", L1, ("angers",)),
    Club("auxerre", "Auxerre", L1, ("auxerre",)),
    Club("brest", "Brest", L1, ("stade brestois",), ("brest",)),
    Club("le-havre", "Le Havre", L1, ("le havre",)),
    Club("lens", "Lens", L1, ("rc lens",), ("lens",)),
    Club("lille", "Lille", L1, ("lille", "losc")),
    Club("lorient", "Lorient", L1, ("lorient",)),
    Club("lyon", "Lyon", L1, ("olympique lyonnais", "lyon")),
    Club("marseille", "Marseille", L1, ("olympique de marseille", "olympique marseille", "marseille")),
    Club("metz", "Metz", L1, ("fc metz",), ("metz",)),
    Club("monaco", "Monaco", L1, ("as monaco",), ("monaco",)),
    Club("nantes", "Nantes", L1, ("fc nantes", "nantes")),
    Club("nice", "Nice", L1, ("ogc nice",), ("nice",)),
    Club("paris-fc", "Paris FC", L1, ("paris fc",)),
    Club("psg", "Paris Saint-Germain", L1, ("paris saint germain", "paris sg", "psg"), subreddit="psg"),
    Club("rennes", "Rennes", L1, ("stade rennais", "rennes")),
    Club("strasbourg", "Strasbourg", L1, ("strasbourg",)),
    Club("toulouse", "Toulouse", L1, ("toulouse",)),

    Club("atlanta-united", "Atlanta United", MLS, ("atlanta united",)),
    Club("austin-fc", "Austin FC", MLS, ("austin fc",)),
    Club("montreal", "CF Montreal", MLS, ("cf montreal",)),
    Club("charlotte-fc", "Charlotte FC", MLS, ("charlotte fc",)),
    Club("chicago-fire", "Chicago Fire", MLS, ("chicago fire",)),
    Club("colorado-rapids", "Colorado Rapids", MLS, ("colorado rapids",)),
    Club("columbus-crew", "Columbus Crew", MLS, ("columbus crew",)),
    Club("dc-united", "D.C. United", MLS, ("dc united",)),
    Club("fc-cincinnati", "FC Cincinnati", MLS, ("fc cincinnati",)),
    Club("fc-dallas", "FC Dallas", MLS, ("fc dallas",)),
    Club("houston-dynamo", "Houston Dynamo", MLS, ("houston dynamo",)),
    Club("inter-miami", "Inter Miami", MLS, ("inter miami",), subreddit="InterMiami"),
    Club("la-galaxy", "LA Galaxy", MLS, ("la galaxy",), subreddit="LAGalaxy"),
    Club("lafc", "LAFC", MLS, ("lafc", "los angeles fc"), subreddit="LAFC"),
    Club("minnesota-united", "Minnesota United", MLS, ("minnesota united",)),
    Club("nashville-sc", "Nashville SC", MLS, ("nashville sc",)),
    Club("new-england-revolution", "New England Revolution", MLS, ("new england revolution",)),
    Club("nycfc", "New York City FC", MLS, ("new york city fc", "nycfc")),
    Club("ny-red-bulls", "New York Red Bulls", MLS, ("new york red bulls", "rbny")),
    Club("orlando-city", "Orlando City", MLS, ("orlando city",)),
    Club("philadelphia-union", "Philadelphia Union", MLS, ("philadelphia union",)),
    Club("portland-timbers", "Portland Timbers", MLS, ("portland timbers",)),
    Club("real-salt-lake", "Real Salt Lake", MLS, ("real salt lake",)),
    Club("san-diego-fc", "San Diego FC", MLS, ("san diego fc",)),
    Club("san-jose-earthquakes", "San Jose Earthquakes", MLS, ("san jose earthquakes",)),
    Club("seattle-sounders", "Seattle Sounders", MLS, ("seattle sounders", "sounders"), subreddit="SoundersFC"),
    Club("sporting-kc", "Sporting Kansas City", MLS, ("sporting kansas city", "sporting kc")),
    Club("st-louis-city", "St. Louis City", MLS, ("st louis city",)),
    Club("toronto-fc", "Toronto FC", MLS, ("toronto fc",)),
    Club("vancouver-whitecaps", "Vancouver Whitecaps", MLS, ("vancouver whitecaps",)),
)

CLUB_BY_ID: dict[str, Club] = {c.id: c for c in CLUBS}
CLUB_SUBREDDITS: dict[str, str] = {c.subreddit.lower(): c.id for c in CLUBS if c.subreddit}
