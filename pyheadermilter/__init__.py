# PyHeader-Milter is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# PyHeader-Milter is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the 
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with PyHeader-Milter.  If not, see <http://www.gnu.org/licenses/>.
#

__all__ = ["HeaderRule", "HeaderMilter"]
name = "pyheadermilter"

import Milter
import argparse
import configparser
import logging
import logging.handlers
import os
import re
import sys

from Milter.utils import parse_addr
from netaddr import IPAddress, IPNetwork, AddrFormatError


class HeaderRule:
    def __init__(self, name, action, header, search=None, value=None, ignore_hosts=[], only_hosts=[], log=True):
        self.logger = logging.getLogger(__name__)
        self.name = name
        self._action = action
        if action == "add":
            self.header = header
        else:
            try:
                self.header = re.compile(header, re.MULTILINE + re.DOTALL + re.IGNORECASE)
            except re.error as e:
                raise RuntimeError("unable to parse option 'header' of rule '{}': {}".format(name, e))
        if action == "mod":
            try:
                self.search = re.compile(search, re.MULTILINE + re.DOTALL + re.IGNORECASE)
            except re.error as e:
                raise RuntimeError("unable to parse option 'search' of rule '{}': {}".format(name, e))
        else:
            self.search = search
        self.value = value
        self.ignore_hosts = []
        try:
            for ignore in ignore_hosts:
                self.ignore_hosts.append(IPNetwork(ignore))
        except AddrFormatError as e:
            raise RuntimeError("unable to parse option 'ignore_hosts' of rule '{}': {}".format(name, e))
        self.only_hosts = []
        try:
            for only in only_hosts:
                self.only_hosts.append(IPNetwork(only))
        except AddrFormatError as e:
            raise RuntimeError("unable to parse option 'only_hosts' of rule '{}': {}".format(name, e))
        self.log = log

    def get_action(self):
        return self._action

    def log_modification(self):
        return self.log

    def ignore_host(self, host):
        ip = IPAddress(host)
        ignore = False
        for ignored in self.ignore_hosts:
            if ip in ignored:
                ignore = True
                break
        if not ignore and self.only_hosts:
            ignore = True
            for only in self.only_hosts:
                if ip in only:
                    ignore = False
                    break
        if ignore:
            self.logger.debug("host {} is ignored by rule {}".format(host, self.name))
        return ignore

    def execute(self, headers):
        modified = []
        if self._action == "add":
            modified.append((self.header, self.value, 0, 1))
        else:
            index = 0
            occurrences = {}
            for name, value in headers:
                if name not in occurrences.keys():
                    occurrences[name] = 1
                else:
                    occurrences[name] += 1
                if self.header.search("{}: {}".format(name, value)):
                    if self._action == "del":
                        value = ""
                    else:
                        value = self.search.sub(self.value, value)
                    modified.append((name, value, index, occurrences[name]))
                index += 1
        return modified


class HeaderMilter(Milter.Base):
    """HeaderMilter based on Milter.Base to implement milter communication"""

    _rules = []

    @staticmethod
    def set_rules(rules):
        HeaderMilter._rules = rules

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        # save rules, it must not change during runtime
        self.rules = HeaderMilter._rules.copy()

    def connect(self, IPname, family, hostaddr):
        self.logger.debug("accepted milter connection from {} port {}".format(*hostaddr))
        ip = IPAddress(hostaddr[0])
        for rule in self.rules.copy():
            if rule.ignore_host(ip):
                self.rules.remove(rule)
        if not self.rules:
            self.logger.debug("host {} is ignored by all rules, skip further processing".format(hostaddr[0]))
            return Milter.ACCEPT
        return Milter.CONTINUE 

    @Milter.noreply
    def data(self):
        self.queueid = self.getsymval('i')
        self.logger.debug("{}: received queue-id from MTA".format(self.queueid))
        self.headers = []
        return Milter.CONTINUE

    @Milter.noreply
    def header(self, name, value):
        self.headers.append((name, value))
        return Milter.CONTINUE

    def eom(self):
        try:
            for rule in self.rules:
                action = rule.get_action()
                log = rule.log_modification()
                self.logger.debug("{}: executing rule '{}'".format(self.queueid, rule.name))
                modified = rule.execute(self.headers)
                for name, value, index, occurrence in modified:
                    if action == "add":
                        if log:
                            self.logger.info("{}: add: header: {}".format(self.queueid, "{}: {}".format(name, value)[0:50]))
                        else:
                            self.logger.debug("{}: add: header: {}".format(self.queueid, "{}: {}".format(name, value)[0:50]))
                        self.headers.insert(0, (name, value))
                        self.addheader(name, value, 1)
                    else:
                        self.chgheader(name, occurrence, value)
                        if action == "mod":
                            if log:
                                self.logger.info("{}: modify: header: {}".format(self.queueid, "{}: {}".format(name, self.headers[index][1])[0:50]))
                            else:
                                self.logger.debug("{}: modify: header (occ. {}): {}".format(self.queueid, occurrence, "{}: {}".format(name, self.headers[index][1])[0:50]))
                            self.headers[index] = (name, value)
                        elif action == "del":
                            if log:
                                self.logger.info("{}: delete: header: {}".format(self.queueid, "{}: {}".format(name, self.headers[index][1])[0:50]))
                            else:
                                self.logger.debug("{}: delete: header (occ. {}): {}".format(self.queueid, occurrence, "{}: {}".format(name, self.headers[index][1])[0:50]))
                            del self.headers[index]
            return Milter.ACCEPT
        except Exception as e:
            self.logger.exception("an exception occured in eom function: {}".format(e))
            return Milter.TEMPFAIL


def main():
    "Run PyHeader-Milter."
    # parse command line
    parser = argparse.ArgumentParser(description="PyHeader milter daemon",
            formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=45, width=140))
    parser.add_argument("-c", "--config", help="Config file to read.", default="/etc/pyheader-milter.conf")
    parser.add_argument("-s", "--socket", help="Socket used to communicate with the MTA.", required=True)
    parser.add_argument("-d", "--debug", help="Log debugging messages.", action="store_true")
    parser.add_argument("-t", "--test", help="Check configuration.", action="store_true")
    args = parser.parse_args()

    # setup logging
    loglevel = logging.INFO
    logname = "pyheader-milter"
    syslog_name = logname
    if args.debug:
        loglevel = logging.DEBUG
        logname = "{}[%(name)s]".format(logname)
        syslog_name = "{}: [%(name)s] %(levelname)s".format(syslog_name)

    # set config files for milter class
    root_logger = logging.getLogger()
    root_logger.setLevel(loglevel)

    # setup console log
    stdouthandler = logging.StreamHandler(sys.stdout)
    stdouthandler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(message)s".format(logname))
    stdouthandler.setFormatter(formatter)
    root_logger.addHandler(stdouthandler)
    logger = logging.getLogger(__name__)
 
    try:
        # read config file
        parser = configparser.ConfigParser()
        if not parser.read(args.config):
            raise RuntimeError("config file not found")

        # check if mandatory config options in global section are present
        if "global" not in parser.sections():
            raise RuntimeError("mandatory section 'global' not present in config file")
        for option in ["rules"]:
            if not parser.has_option("global", option):
                raise RuntimeError("mandatory option '{}' not present in config section 'global'".format(option))

        # read global config section
        global_config = dict(parser.items("global"))

        # read active rules
        active_rules = [ r.strip() for r in global_config["rules"].split(",") ]
        if len(active_rules) != len(set(active_rules)):
            raise RuntimeError("at least one rule is specified multiple times in 'rules' option")
        if "global" in active_rules:
            active_rules.remove("global")
            logger.warning("removed illegal rule name 'global' from list of active rules")
        if not active_rules:
            raise RuntimeError("no rules configured")

        logger.debug("preparing milter configuration ...")
        rules = []
        # iterate active rules
        for rule_name in active_rules:
            # check if config section exists 
            if rule_name not in parser.sections():
                raise RuntimeError("config section '{}' does not exist".format(rule_name))
            config = dict(parser.items(rule_name))

            # check if mandatory option action is present in config
            option = "action"
            if option not in config.keys() and \
                    option in global_config.keys():
                config[option] = global_config[option]
            if option not in config.keys():
                raise RuntimeError("mandatory option '{}' not specified for rule '{}'".format(option, rule_name))
            config["action"] = config["action"].lower()
            if config["action"] not in ["add", "del", "mod"]:
                raise RuntimeError("invalid action specified for rule '{}'".format(rule_name))

            # check if mandatory options are present in config
            mandatory = ["header"]
            if config["action"] == "add":
                mandatory += ["value"]
            elif config["action"] == "mod":
                mandatory += ["search", "value"]
            for option in mandatory:
                if option not in config.keys() and \
                        option in global_config.keys():
                    config[option] = global_config[option]
                if option not in config.keys():
                    raise RuntimeError("mandatory option '{}' not specified for rule '{}'".format(option, rule_name))

            # check if optional config options are present in config
            defaults = {
                "ignore_hosts": [],
                "only_hosts": [],
                "log": "true"
            }
            for option in defaults.keys():
                if option not in config.keys() and \
                        option in global_config.keys():
                    config[option] = global_config[option]
                if option not in config.keys():
                    config[option] = defaults[option]
            if config["ignore_hosts"]:
                config["ignore_hosts"] = [ h.strip() for h in config["ignore_hosts"].split(",") ]
            if config["only_hosts"]:
                config["only_hosts"] = [ h.strip() for h in config["only_hosts"].split(",") ]
            config["log"] = config["log"].lower()
            if config["log"] == "true":
                config["log"] = True
            elif config["log"] == "false":
                config["log"] = False
            else:
                raise RuntimeError("invalid value specified for option 'log' for rule '{}'".format(rule_name))
            # add rule
            logging.debug("adding rule '{}'".format(rule_name))
            rules.append(HeaderRule(name=rule_name, **config))

    except RuntimeError as e:
        logger.error(e)
        sys.exit(255)

    if args.test:
        print("Configuration ok")
        sys.exit(0)

    # change log format for runtime
    formatter = logging.Formatter("%(asctime)s {}: [%(levelname)s] %(message)s".format(logname), datefmt="%Y-%m-%d %H:%M:%S")
    stdouthandler.setFormatter(formatter)

    # setup syslog
    sysloghandler = logging.handlers.SysLogHandler(address="/dev/log", facility=logging.handlers.SysLogHandler.LOG_MAIL)
    sysloghandler.setLevel(loglevel)
    formatter = logging.Formatter("{}: %(message)s".format(syslog_name))
    sysloghandler.setFormatter(formatter)
    root_logger.addHandler(sysloghandler)

    logger.info("PyHeader-Milter starting")
    HeaderMilter.set_rules(rules)

    # register milter factory class
    Milter.factory = HeaderMilter
    Milter.set_exception_policy(Milter.TEMPFAIL)

    rc = 0
    try:
        Milter.runmilter("pyheader-milter", socketname=args.socket, timeout=30)
    except Milter.milter.error as e:
        logger.error(e)
        rc = 255
    logger.info("PyHeader-Milter terminated")
    sys.exit(rc)


if __name__ == "__main__":
    main()