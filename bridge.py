import json, queue, subprocess, sys, threading, time
import requests

# ------------------------------------------------------------------------

config = None
headers = None
engine = None
active_game = None
active_game_MUTEX = threading.Lock()
main_log = queue.Queue()

# ------------------------------------------------------------------------

class Engine():

	def __init__(self, command, shortname):

		self.shortname = shortname

		self.process = subprocess.Popen(command, shell = False,
										stdin = subprocess.PIPE,
										stdout = subprocess.PIPE,
										stderr = subprocess.PIPE,
										)

		# Make a thread that puts this engine's stdout onto a queue which we read when needed...
		# I forget why I bothered with this now. Seems redundant. I was probably worried about
		# the pipe filling up.

		self.stdout_queue = queue.Queue()
		threading.Thread(target = stdout_to_queue, args = (self.process, self.stdout_queue, self.shortname), daemon = True).start()

		# Make a thread that puts this engine's stderr into a log...
		# I think it's necessary to do SOMETHING with stderr (so it doesn't build up and hang the engine).
		# As an alternative, we could send it to devnull.

		threading.Thread(target = stderr_to_log, args = (self.process, "{}_stderr.txt".format(self.shortname)), daemon = True).start()

	def send(self, msg):

		msg = msg.strip()
		b = bytes(msg + "\n", encoding = "ascii")
		self.process.stdin.write(b)
		self.process.stdin.flush()
		log(self.shortname + " <- " + msg)

	def get_best_move(self):

		# Assumes the go command has already been sent

		while 1:
			z = self.stdout_queue.get()
			# log(self.shortname + " :: " + z)

			if "bestmove" in z:
				tokens = z.split()
				return tokens[1]

# ------------------------------------------------------------------------

class Game():

	def __init__(self, gameId):

		self.gameId = gameId
		self.gameFull = None
		self.colour = None
		self.events = requests.get("https://lichess.org/api/bot/game/stream/{}".format(gameId), headers = headers, stream = True)

		self.response_times = dict()		# For chat timeouts: command --> time
		self.chat_handlers = dict()

		all_cmd_methods = [x for x in dir(self) if x[:4] == "say_"]

		for method in all_cmd_methods:
			self.chat_handlers["!" + method[4:]] = getattr(self, method)


	def loop(self):

		for line in self.events.iter_lines():

			if not line:					# Filter out keep-alive newlines
				continue

			dec = line.decode('utf-8')
			j = json.loads(dec)

			if j["type"] == "gameFull":

				self.gameFull = j

				log(j)
				
				# The following lines can fail if the opponent is the built-in AI, hence try...

				try:
					if j["white"]["name"].lower() == config["account"].lower():
						self.colour = "white"
				except:
					pass

				try:
					if j["black"]["name"].lower() == config["account"].lower():
						self.colour = "black"
				except:
					pass

				self.handle_state(j["state"])

			elif j["type"] == "gameState":
				self.handle_state(j)

			elif j["type"] == "chatLine":
				self.handle_chat(j)

		log("Game stream closed...")
		self.finish()


	def handle_state(self, state):

		if self.colour == None:
			log("ERROR: handle_state() called but my colour is unknown")
			self.abort()

		moves = []

		if state["moves"]:
			moves = state["moves"].split()

		if len(moves) % 2 == 0 and self.colour == "black":
			return
		if len(moves) % 2 == 1 and self.colour == "white":
			return

		if len(moves) > 0:
			log("-----------------")
			log("Opponent played {}".format(moves[-1]))

		self.play(state)


	def play(self, state):

		global engine

		log("-----------------")

		engine.send("position {} moves {}".format(self.gameFull['initialFen'], state['moves']))
		engine.send("go wtime {} btime {} winc {} binc {}".format(state["wtime"], state["btime"], state['winc'], state['binc']))

		move = engine.get_best_move()
		log("Playing {}".format(move))
		self.move(move)


	def resign(self):

		log("Resigning game {}".format(self.gameId))

		requests.post("https://lichess.org/api/bot/game/{}/resign".format(self.gameId), headers = headers)
		if r.status_code != 200:
			try:
				log(r.json())
			except:
				log("resign returned {}".format(r.status_code))

		self.finish()


	def abort(self):

		log("Aborting game {}".format(self.gameId))

		r = requests.post("https://lichess.org/api/bot/game/{}/abort".format(self.gameId), headers = headers)
		if r.status_code != 200:
			try:
				log(r.json())
			except:
				log("abort returned {}".format(r.status_code))

		self.finish()


	def move(self, move):		# move in UCI format

		r = requests.post("https://lichess.org/api/bot/game/{}/move/{}".format(self.gameId, move) , headers = headers)
		if r.status_code != 200:
			try:
				log(r.json())
			except:
				log("move returned {}".format(r.status_code))


	def tell_spectators(self, msg):

		# Post is in x-www-form-urlencoded, which requests does by default (non-JSON)

		data = {"room": "spectator", "text": msg}

		r = requests.post("https://lichess.org/api/bot/game/{}/chat".format(self.gameId), data = data, headers = headers)
		if r.status_code != 200:
			try:
				log(r.json())
			except:
				log("Talking to chat returned {}".format(r.status_code))


	def finish(self):

		global active_game
		global active_game_MUTEX

		with active_game_MUTEX:
			if active_game == self:
				active_game = None
				log("active_game set to None")
				log("-----------------------------------------------------------------")
			else:
				log("active_game not touched")


	def handle_chat(self, j):

		msg = j["text"]

		if msg in self.chat_handlers:	# and j["room"] == "spectator":

			last_response = self.response_times.get(msg)

			if last_response == None or time.monotonic() - last_response > 10:
				self.chat_handlers[msg]()
				self.response_times[msg] = time.monotonic()


	# All chat handlers should be named say_foo so they can be found by __init__()

	def say_commands(self):

		commands = sorted([key for key in self.chat_handlers])
		self.tell_spectators("Known commands: " + " ".join(commands))

# ------------------------------------------------------------------------

def main():

	global config
	global headers
	global engine

	# Load config file...

	try:
		with open("config.json") as config_file:
			config = json.load(config_file)
			for prop in ["account", "token", "command", "extras"]:
				if prop not in config:
					print("config.json did not have needed '{}' property".format(prop))
					sys.exit()

	except FileNotFoundError:
		print("Couldn't load config.json")
		sys.exit()

	except json.decoder.JSONDecodeError:
		print("config.json seems to be illegal JSON")
		sys.exit()

	headers = {"Authorization": "Bearer {}".format(config['token'])}

	# Start logging...

	threading.Thread(target = logger_thread, args = ("log.txt", main_log), daemon = True).start()
	log("-- STARTUP -- at {} ".format(time.strftime('%a, %d %b %Y %H:%M:%S', time.localtime())) + "-" * 40)

	# Start engines...

	engine = Engine(config["command"], "eng")
	engine.send("uci")

	for extra in config["extras"]:
		engine.send(extra)

	# Connect to Lichess API...

	event_stream = requests.get("https://lichess.org/api/stream/event", headers = headers, stream = True)

	for line in event_stream.iter_lines():

		if line:

			dec = line.decode('utf-8')
			j = json.loads(dec)

			if j["type"] == "challenge":
				handle_challenge(j["challenge"])

			if j["type"] == "gameStart":
				start_game(j["game"]["id"])

	log("ERROR: Main event stream closed!")


def handle_challenge(challenge):

	global active_game
	global active_game_MUTEX

	log("Incoming challenge from {} -- {} (rated: {})".format(challenge['challenger']['name'], challenge['timeControl']['show'], challenge['rated']))

	accepting = True

	# Already playing...

	with active_game_MUTEX:
		if active_game:
			accepting = False

	# Variants...

	if challenge["variant"]["key"] != "standard":
		accepting = False

	# Time control...

	if challenge["timeControl"]["type"] != "clock":
		accepting = False
	elif challenge["timeControl"]["limit"] > 300:
		accepting = False

	if not accepting:
		decline(challenge["id"])
	else:
		accept(challenge["id"])


def decline(challengeId):

	log("Declining challenge {}".format(challengeId))
	r = requests.post("https://lichess.org/api/challenge/{}/decline".format(challengeId), headers = headers)
	if r.status_code != 200:
		try:
			log(r.json())
		except:
			log("decline returned {}".format(r.status_code))

def accept(challengeId):

	log("Accepting challenge {}".format(challengeId))
	r = requests.post("https://lichess.org/api/challenge/{}/accept".format(challengeId), headers = headers)
	if r.status_code != 200:
		try:
			log(r.json())
		except:
			log("accept returned {}".format(r.status_code))

def start_game(gameId):

	global active_game
	global active_game_MUTEX

	game = Game(gameId)
	autoabort = False

	with active_game_MUTEX:
		if active_game:
			autoabort = True
		else:
			active_game = game

	if autoabort:
		log("WARNING: game started but I seem to be in a game")
		game.abort()
		return

	threading.Thread(target = runner, args = (game, )).start()
	log("Game {} started".format(gameId))


def runner(game):
	global engine
	engine.send("ucinewgame")
	game.loop()


def sign(num):
	if num < 0:
		return -1
	if num > 0:
		return 1
	return 0


def log(msg):
	main_log.put(msg)


def stdout_to_queue(process, q, shortname):

	while 1:
		z = process.stdout.readline().decode("utf-8")

		if z == "":
			log("WARNING: got EOF while reading from {}".format(shortname))
			return
		elif z.strip() == "":
			pass
		else:
			q.put(z.strip())


def stderr_to_log(process, filename):

	logfile = open(filename, "a")

	while 1:
		z = process.stderr.readline().decode("utf-8")

		if z == "":
			logfile.write("EOF" + "\n")
			return
		else:
			logfile.write(z)


def logger_thread(filename, q):

	logfile = open(filename, "a")

	flush_time = time.monotonic()

	while 1:

		try:

			msg = q.get(block = False)

			msg = str(msg).strip()
			logfile.write(msg + "\n")
			print(msg)

		except queue.Empty:

			if time.monotonic() - flush_time > 1:
				logfile.flush()
				flush_time = time.monotonic()

			time.sleep(0.1)		# Essential since we're not blocking on the read.

# ------------------------------------------------------------------------

if __name__ == "__main__":
	main()
