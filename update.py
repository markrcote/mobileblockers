import ConfigParser
import datetime
import json
import os
import pytz
import sqlite3
import tempita
import urllib
import urllib2
from collections import defaultdict

# FIXME: Get the real local time zone...
LOCAL_TZ = 'US/Eastern'

BZAPI = 'https://api-dev.bugzilla.mozilla.org/1.1/count?'
BZ = 'https://bugzilla.mozilla.org/buglist.cgi?'


API_BZ_FIELD_MAP = { 'changed_field': 'chfield',
                     'changed_field_to': 'chfieldvalue',
                     'changed_before': 'chfieldto',
                     'changed_after': 'chfieldfrom' }


API_BZ_VALUE_MAP = { 'contains': 'substring',
                     'not_contains': 'notsubstring' }


def api_to_bz(args):
    newargs = {}
    for name, value in args.iteritems():
        newargs[API_BZ_FIELD_MAP.get(name, name)] = \
            API_BZ_VALUE_MAP.get(value, value)
    return newargs


class QueryArgs(object):

    args = {}
    b = 0

    def __init__(self, initial_args):
        if initial_args:
            self.args = initial_args

    def add_user_query(self, field, type, usernames):
        for count, u in enumerate(usernames):
            self.args['field0-%d-%d' % (self.b, count)] = field
            self.args['type0-%d-%d' % (self.b, count)] = type
            self.args['value0-%d-%d' % (self.b, count)] = u
        self.b += 1

    def add_user_assigned_query(self, usernames):
        if usernames:
            self.add_user_query('assigned_to', 'equals', usernames)

    def add_blocker_query(self, value='+'):
        self.args['field0-%d-0' % self.b] = 'cf_blocking_fennec10'
        self.args['type0-%d-0' % self.b] = 'contains'
        self.args['value0-%d-0' % self.b] = value
        self.b += 1

    def add_nonblocker_query(self):
        self.args['field0-%d-0' % self.b] = 'cf_blocking_fennec10'
        self.args['type0-%d-0' % self.b] = 'not_contains'
        self.args['value0-%d-0' % self.b] = '+'
        self.b += 1
        self.args['field0-%d-0' % self.b] = 'cf_blocking_fennec10'
        self.args['type0-%d-0' % self.b] = 'not_contains'
        self.args['value0-%d-0' % self.b] = 'soft'
        self.b += 1


def open_blockers(usernames=None):
    args = QueryArgs({ 'resolution': '---' })
    args.add_blocker_query()
    args.add_user_assigned_query(usernames)
    return args.args


def open_softblockers(usernames=None):
    args = QueryArgs({ 'resolution': '---' })
    args.add_blocker_query('soft')
    args.add_user_assigned_query(usernames)
    return args.args


def nonblockers_fixed(usernames=None, before='Now', after='-1D'):
    if not usernames:
        return {}
    args = QueryArgs({ 'changed_field': 'resolution',
                       'changed_field_to': 'FIXED',
                       'changed_before': before,
                       'changed_after': after })
    args.add_nonblocker_query()
    args.add_user_assigned_query(usernames)
    return args.args


def blockers_fixed(usernames=None, before='Now', after='-1D'):
    args = QueryArgs({ 'changed_field': 'resolution',
                       'changed_field_to': 'FIXED',
                       'changed_before': before,
                       'changed_after': after })
    args.add_blocker_query()
    args.add_user_assigned_query(usernames)
    return args.args


def softblockers_fixed(usernames=None, before='Now', after='-1D'):
    args = QueryArgs({ 'changed_field': 'resolution',
                       'changed_field_to': 'FIXED',
                       'changed_before': before,
                       'changed_after': after })
    args.add_blocker_query('soft')
    args.add_user_assigned_query(usernames)
    return args.args


def get_teams():
    teams = {}
    cfg = ConfigParser.RawConfigParser()
    cfg.read('teams.conf')
    for s in cfg.sections():
        teams[s] = {}
        for name, emails in cfg.items(s):
            teams[s][name] = [x.strip() for x in emails.split(',')]
    return teams


def get_bzapi_urls(func, kwargs):
    urls = {}
    teams = get_teams()
    teams['-'] = {'-': []}
    for team, members in teams.iteritems():
        for member, emails in members.iteritems():
            _kwargs = { 'usernames': emails }
            _kwargs.update(kwargs)
            url_args = func(**_kwargs)
            if not url_args:
                continue
            urls[member] = BZAPI + '&'.join(['='.join([urllib.quote(x) for x in y]) for y in url_args.viewitems()])
    return urls


def get_db():
    con = sqlite3.connect('mobileblockers.db')
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    tables = [x[0] for x in cur.execute("select name from sqlite_master where type='table'")]
    if not 'team' in tables:
        cur.execute('create table team (name text)')
        cur.execute('insert into team values ("-")')
    if not 'member' in tables:
        cur.execute('create table member (name text, team int, foreign key (team) references team(rowid))')
        cur.execute('insert into member values ("-", 1)')
    if not 'count' in tables:
        cur.execute('create table count (date text, updated text, openblockers int default 0, opensoftblockers int default 0, closedblockers int default 0, closednonblockers int default 0, closedsoftblockers int default 0, member int default 0, foreign key (member) references member(rowid))')
    teams = get_teams()
    for team, members in teams.iteritems():
        if cur.execute('select count(rowid) from team where name=?', (team,)).fetchone()[0] == 0:
            cur.execute('insert into team values (?)', (team,))
        team_id = cur.execute('select rowid from team where name=?', (team,)).fetchone()[0]
        for member in members.keys():
            if cur.execute('select count(rowid) from member where name=?', (member,)).fetchone()[0] == 0:
                cur.execute('insert into member (name, team) values (?, ?)', (member, team_id))
    con.commit()
    return con


def update_count(cur, date, member, count_type, count):
    now = datetime.datetime.now()
    member_id = cur.execute('select rowid from member where name=?', (member,)).fetchone()[0]
    r = cur.execute('select rowid from count where member=? and date=?', (member_id, date)).fetchone()
    if r:
        cur.execute('update count set %s=?,updated=? where member=? and date=?' % count_type, (count, now, member_id, date))
    else:
        cur.execute('insert into count (date, updated, %s, member) values (?, ?, ?, ?)' % count_type, (date, now, count, member_id))


# "states" are searches for current values. these can only be *snapshotted*
# at the current time; past values are more-or-less impossible to obtain
# (although the entire history for a bug could be retrieved and parsed).
# mapping of column names to functions
STATES = { 'openblockers': open_blockers,
           'opensoftblockers': open_softblockers }

# "transitions" are searches for changed fields. these can be obtained for
# any date range.
# mapping of column names to functions
TRANSITIONS = { 'closedblockers': blockers_fixed,
                'closednonblockers': nonblockers_fixed,
                'closedsoftblockers': softblockers_fixed }


def update_states():
    print 'updating states'
    now = datetime.datetime.now(pytz.timezone(LOCAL_TZ)).astimezone(pytz.timezone('US/Pacific'))
    today = now.date()
    con = get_db()
    cur = con.cursor()
    for column, func in STATES.iteritems():
        urls = get_bzapi_urls(func, {})
        for member, url in urls.iteritems():
            print url
            count = int(json.loads(urllib2.urlopen(url).read())['data'])
            update_count(cur, today, member, column, count)
    con.commit()


def update_transitions(date_from, date_to, only_transitions=[]):
    now = datetime.datetime.now()
    con = get_db()
    cur = con.cursor()
    after = date_from
    before = date_from + datetime.timedelta(days=1)
    transitions = []
    if only_transitions:
        transitions = [(x, TRANSITIONS[x]) for x in only_transitions]
    else:
        transitions = [(x, y) for x, y in TRANSITIONS.iteritems()]
    while after <= date_to:
        for column, func in transitions:
            print 'updating transition %s' % column
            kwargs = { 'before': str(before), 'after': str(after) }
            urls = get_bzapi_urls(func, kwargs)
            for member, url in urls.iteritems():
                print url
                count = int(json.loads(urllib2.urlopen(url).read())['data'])
                update_count(con, after, member, column, count)
        after = before
        before = before + datetime.timedelta(days=1)
    con.commit()
    

class Member(object):

    def __init__(self, name, usernames):
        self.name = name
        self.usernames = usernames
        self.id = 0
        self.rows = []
        self.bzlinks = defaultdict(list)
        self.stats = defaultdict(dict)

    def load(self, cursor):
        self.id = cursor.execute('select rowid from member where name=?', (self.name,)).fetchone()[0]
        today = datetime.date.today()
        self.name = cursor.execute('select name from member where rowid=?', (self.id,)).fetchone()[0]

        total = defaultdict(int)
        self.rows = []
        rows = cursor.execute('select * from count where member=? order by date asc', (self.id,))
        for r in rows:
            row = dict(r)
            self.rows.append(row)
            for column, func in TRANSITIONS.iteritems():
                if (not 'low' in self.stats[column] or
                    r[column] < self.stats[column]['low']):
                    self.stats[column]['low'] = r[column]
                if (not 'high' in self.stats[column] or
                    r[column] > self.stats[column]['high']):
                    self.stats[column]['high'] = r[column]
                total[column] += r[column]
                d = datetime.datetime.strptime(r['date'], '%Y-%m-%d').date()
                row['relative_day'] = '%dD' % ((d - today).days)
                args = api_to_bz(func(self.usernames, before=(d + datetime.timedelta(days=1)).strftime('%Y-%m-%d'), after=r['date']))
                self.bzlinks[column].append((r[column], BZ + '&'.join(['='.join([urllib.quote(x) for x in y]) for y in args.viewitems()])))
        for column in TRANSITIONS.keys():
            self.stats[column]['mean'] = '%0.2f' % (float(total[column]) /
                                                    len(self.rows))


class IndexChart(object):

    def __init__(self, id, title):
        self.id = id
        self.title = title
        self.rows = []
        self.bzlinks = []
        self.stats = {'mean': None, 'low': None, 'high': None}

    def get_rows(self, start):
        return [x for x in self.rows if x['date'] >= start]

    def calc_stats(self):
        total = 0
        for r in self.rows:
            total += r['count']
            if self.stats['low'] is None or r['count'] < self.stats['low']:
                self.stats['low'] = r['count']
            if self.stats['high'] is None or r['count'] > self.stats['high']:
                self.stats['high'] = r['count']
        self.stats['mean'] = '%0.2f' % (float(total) / len(self.rows))


def load_index_charts(cursor):
    today = datetime.date.today()
    charts = { 'openblockers': IndexChart('openblockers', 'Open Blockers'),
               'opensoftblockers': IndexChart('opensoftblockers', 'Open Soft Blockers'),
               'closedblockers': IndexChart('closedblockers', 'Closed Blockers'),
               'closedsoftblockers': IndexChart('closedsoftblockers', 'Closed Soft Blockers') }
    rows = cursor.execute('select * from count where member=1 order by date asc')
    for r in rows:
        d = datetime.datetime.strptime(r['date'], '%Y-%m-%d').date()
        rel_day = '%dD' % ((d - today).days)
        for key, func in (('openblockers', open_blockers),
                          ('opensoftblockers', open_softblockers),
                          ('closedblockers', blockers_fixed),
                          ('closedsoftblockers', softblockers_fixed)):
            charts[key].rows.append({'date': r['date'], 'relative_day': rel_day, 'count': r[key]})
            args = {}
            if key.startswith('open') and rel_day == '0D':
                args = api_to_bz(func(None))
            elif not key.startswith('open'):
                args = api_to_bz(func(None, before=(d + datetime.timedelta(days=1)).strftime('%Y-%m-%d'), after=r['date']))
            if args:
                charts[key].bzlinks.append((r[key], BZ + '&'.join(['='.join([urllib.quote(x) for x in y]) for y in args.viewitems()])))
    for chart in charts.values():
        chart.calc_stats()
    return (charts['openblockers'], charts['opensoftblockers'],
            charts['closedblockers'], charts['closedsoftblockers'])


def produce_index(last_update):
    con = get_db()
    cur = con.cursor()
    teams = get_teams()
    team_list = []
    for team_name in teams.keys():
        team_list.append({'url': '%s.html' % team_name.replace(' ', '').replace('/', ''),
                          'name': team_name})
    team_list.sort(cmp=lambda x,y: cmp(x['name'], y['name']))
    charts = load_index_charts(cur)
    for template in ('index.html.tmpl', 'big.html.tmpl'):
        tmpl = tempita.HTMLTemplate.from_filename(template)
        f = file(os.path.splitext(template)[0], 'w')
        f.write(tmpl.substitute(charts=charts, teams=team_list,
                                last_update=last_update))
        f.close()
        

def produce_team_pages(last_update):
    tmpl = tempita.HTMLTemplate.from_filename('team.html.tmpl')
    con = get_db()
    cur = con.cursor()
    teams = get_teams()
    all_members = []
    for team_name, team_members in teams.iteritems():
        print team_name
        members = []
        for member_name, usernames in team_members.iteritems():
            m = Member(member_name, usernames)
            m.load(cur)
            members.append(m)
            all_members.append(m)
        page_name = '%s.html' % team_name.replace(' ', '').replace('/', '')
        f = file(page_name, 'w')
        f.write(tmpl.substitute(team_name=team_name, members=members,
                                last_update=last_update))
        f.close()
    return all_members


def main(options, start, end):
    # FIXME: Probably shouldn't set the last-update time if we're only
    # producing output, but meh.
    last_update = datetime.datetime.now(pytz.timezone(LOCAL_TZ)).astimezone(pytz.timezone('US/Pacific')).strftime('%Y-%m-%d %H:%M %Z')
    if not options.output_only:
        print 'updating from %s to %s' % (start, end)
        if options.states:
            update_states()
        if options.transitions:
            update_transitions(start, end, options.only_transitions)
    produce_team_pages(last_update)
    produce_index(last_update)

            
if __name__ == '__main__':
    from optparse import OptionParser
    parser = OptionParser()
    parser.add_option('--output-only', action='store_true', dest='output_only',
                      default=False, help='just output pages; don\'t update db')
    parser.add_option('--no-states', action='store_false',
                      dest='states', default=True,
                      help='don\'t query for states (e.g. open)')
    parser.add_option('--no-transitions', action='store_false',
                      dest='transitions', default=True,
                      help='don\'t query for transitions (e.g. open->closed)')
    parser.add_option('--transition', action='append', dest='only_transitions',
                      type='string', default=[],
                      help='update only this transition (implies --no-states); '
                      'can be specified multiple times')
    (options, args) = parser.parse_args()

    if options.only_transitions:
        options.states = False

    # if no date is given, we want the last full day (e.g. yesterday, Pacific
    # time).
    if len(args) > 0:
        start = datetime.datetime.strptime(args[0], '%Y-%m-%d').date()
    else:
        start = (datetime.datetime.now(pytz.timezone(LOCAL_TZ)).astimezone(pytz.timezone('US/Pacific')) - datetime.timedelta(days=1)).date()
    if len(args) > 1:
        end = datetime.datetime.strptime(args[1], '%Y-%m-%d').date()
    else:
        end = (datetime.datetime.now(pytz.timezone(LOCAL_TZ)).astimezone(pytz.timezone('US/Pacific')) - datetime.timedelta(days=1)).date()
    main(options, start, end)
