#!/usr/bin/python
# -*- coding: utf-8 -*-

#Esto lee cada cuarto y lo envía al servidor MQTT. No usa el receptor general, sino el de planta/datos (es necesario para alarmas)
#Versión que hace peticiones directamente los Solarmax por puerto serie.
#Tiene un parche para el inverter 31 porque no resetea el KDY al cambiar el día, sino al rato de empezar a producir.


import sys
import time
import xml.etree.ElementTree as ET
import paho.mqtt.client as mqttClient
import json
import select
from contextlib import closing
from datetime import datetime
import configparser as ConfigParser
import serial
import threading
import termios
import tty
import sqlite3 as lite


_publish_condition = threading.Condition()
_pending_publish_events = {}


IDC = "IDC"	  # DC Current
UL1 = "UL1"	  # Voltage Phase 1
UL2 = "UL2"	  # Voltage Phase 2
UL3 = "UL3"	  # Voltage Phase 3
TKK = "TKK"	  # inverter operating temp
IL1 = "IL1"	  # current phase 1
IL2 = "IL2"	  # current phase 2
IL3 = "IL3"	  # current phase 3
SYS = "SYS"	  # 4E28 = 17128
TNF = "TNF"	  # generated frequency (Hz)
UDC = "UDC"	  # DC voltage (V DC)
PAC = "PAC"	  # AC power being generated * 2 (W)
PRL = "PRL"	  # relative output (%)
KT0 = "KT0"	  # total yield (kWh)
KDY = "KDY"
KMT = "KMT"
SAL = "SAL"
UGD = "UGD"	  

QUERY_DICT = {
	'KDY': 'Energy today (kWh)', 'KDL': 'Energy yesterday (Wh)', 'KYR': 'Energy this year (kWh)', 'KLY': 'Energy last year (kWh)',
	'KMT': 'Energy this month (kWh)', 'KLM': 'Energy last month (kWh)', 'KT0': 'Total Energy(kWh)', 'IL1': 'AC Current Phase 1 (A)',
	'IL2': 'AC Current Phase 2 (A)', 'IL3': 'AC Current Phase 3 (A)', 'IDC': 'DC Current (A)', 'PAC': 'AC Power (W)',
	'PDC': 'DC Power (W)', 'PRL': 'Relative power (%)', 'TNP': 'Grid period duration',
	'TNF': 'Generated Frequency (Hz)', 'TKK': 'Inverter Temperature (C)', 'UL1': 'AC Voltage Phase 1 (V)',
	'UL2': 'AC Voltage Phase 2 (V)', 'UL3': 'AC Voltage Phase 3 (V)', 'UDC': 'DC Voltage (V)',
	'UD01': 'String 1 Voltage (V)', 'UD02': 'String 2 Voltage (V)', 'UD03': 'String 3 Voltage (V)',
	'ID01': 'String 1 Current (A)', 'ID02': 'String 2 Current (A)', 'ID03': 'String 3 Current (A)',
	'ADR': 'Address', 'TYP': 'Type', 'PIN': 'Installed Power (W)', 'CAC': 'Start Ups (?)', 'KHR': 'Operating Hours',
	'SWV': 'Software Version', 'DDY': 'Date day', 'DMT': 'Date month', 'DYR': 'Date year', 'THR': 'Time hours',
	'TMI': 'Time minutes', 'LAN': 'Language',
	'SAL': 'System Alarms', 'SYS': 'System Status',
	'MAC': 'MAC Address', 'EC00': 'Error Code 0', 'EC01': 'Error Code 1', 'EC02': 'Error Code 2', 'EC03': 'Error Code 3',
	'EC04': 'Error Code 4', 'EC05': 'Error Code 5', 'EC06': 'Error Code 6', 'EC07': 'Error Code 7', 'EC08': 'Error Code 8',
	'BDN': 'Build number',
	'DIN': '?',
	'SDAT': 'datetime ?', 'FDAT': 'datetime ?',
	'U_AC': '?', 'F_AC': 'Grid Frequency', 'SE1': '',
	'U_L1L2': 'Phase1 to Phase2 Voltage (V)', 'U_L2L3': 'Phase2 to Phase3 Voltage (V)', 'U_L3L1': 'Phase3 to Phase1 Voltage (V)',
	'UGD': 'UGND'
}


def on_connect(client, userdata, flags, rc):
	global Connected
	Connected = rc == 0
	if not Connected:
		print("Connection failed")


def on_publish(client, userdata, mid, *args):
	with _publish_condition:
		event = _pending_publish_events.get(mid)
		if event is not None:
			event.set()


def hex_checksum(line):
	"""Return the HEX Checksum"""
	total = 0
	for char in line:
		total += ord(char)
	# Take the hex value
	check = format(total, '04X')
	return check


# required_values = must be a LIST
# inverter_address = MUST be an INT
def construct_inverter_query(inverter_address, required_values):
	"""build the Solarmax inverter query string"""
	error = False

	# convert INT address to HEX, as 2 digit in uppercase
	hex_addr = format(inverter_address, '02X')

	# Check that each type is valid
	for value in required_values:
		if value not in QUERY_DICT:
			print("Value: {} not known".format(value))
			error = True

	# If any errors, then return
	if error:
		return False

	# Query line will look something like this:
	# {FB;05;4E|64:E1D;E11;E1h;E1m;E1M;E2D;E21;E2h;E2m;E2M;E3D;E31;E3h;E3m;E3M|1270}
	# See this for message format -> https://blog.dest-unreach.be/2009/04/15/solarmax-maxtalk-protocol-reverse-engineered

	# Construct the line as follows:
	#
	# 1. Join the required_values into a string separated by semicolons
	joined_values = ';'.join(required_values)
	# 2. Enclose the values in '64:' and the pipe symbols
	query_string = '|64:' + joined_values + '|'
	# 3. Determine the length of the whole string, in HEX
	#		So its {AA;BB;CC + query_string + XXXX} ... where AA,BB,CC are always 2 chars, and XXXX is 4 chars
	query_length = 9 + len(query_string) + 5
	query_length_hex = format(query_length, '02X')
	# 4. Add the header to the query
	query_string = 'FB;' + hex_addr + ';' + query_length_hex + query_string
	# 5. Do a checksum on the whole lot and add it to the query string
	check = hex_checksum(query_string)
	query_string = query_string + check
	# 6. Enclose the whole lot in curly braces
	query_string = '{' + query_string + '}'

	return query_string




def genData(s):
	""" takes a pair: <field>=<0xdata> and converts to a list
	with the name mapped using field_map and value converted to base 10 and appropriately scaled """
	t = s.split('=')
	if len(t)>1:
		try:		
			f = t[0]
			if (f == 'SYS'):   # remove the trailing ,0
				v = int(t[1][:t[1].find(',')], 16)
			else:
				v = int(t[1], 16)
			if f in ('PAC', 'PDC'):
				v = float(v) /2.0
			if (f == 'IDC' or f == 'TNF'):	# current & frequency needs to be divided by 100
				v = v/100.0
			elif f in ('IL2', 'IL1', 'IL3', 'IDC', 'TNF', 'ID01', 'ID02', 'ID03'):
				v = float(v)/100.0
			elif f in ('UL2', 'UL1', 'UL3','UGD', 'KDY', 'UD01', 'UD02', 'UD03'):
				v = float(v)/10.0
			#elif (f == 'SAL'):
					#      v=256
		except:
			return None			
		try:	 
			return [f, v]
		except:
			return None	   
	else:
		return None

def convert_data(data):
	global planta
	# pull out the data elements into a list  and replace the code with the json field name
	ev = [genData(s) for s in data[data.find(':')+1:data.find('|', data.find(':'))].split(';')]
	#Le quito los que sean None por si algún dato no existe
	ev = list(filter(None, ev))
	inverter=int(data[1:3],16)
	ev.insert(0,['Planta',planta])
	linea=0
	ev.insert(1,['Linea',linea])
	ev.insert(2,['Inverter',inverter])
	#Tengo que pasar la fecha
	now=datetime.now()
	fecha_ahora=now.strftime("%Y-%m-%d %H:%M:%S")
	ev.insert(3,['Fecha',fecha_ahora])
	return (dict(ev))


def read_inverter(query_string, serial_port, debug):
	"""Send the query to the inverter and read the response"""

	# Flush the inputs and outputs
	serial_port.flushInput()
	serial_port.flushOutput()

	# Encode the command
	if debug > 1:
		print("Sending the command to the RS485 port: {}".format(query_string))
	serial_port.write(bytes(query_string,encoding='utf-8'))

	# wait 1 second. Do not make it less than that
	time.sleep(1)

	# read answer line
	response = ''
	while serial_port.inWaiting() > 0:
		response += serial_port.readline().decode(encoding="utf-8",errors="ignore")

	if debug > 1:
		print("Received the following data: {}".format(response))

	return response


def open_serial_port(serial_dev):
	"""open serial port"""
	serial_port = serial.Serial(port=serial_dev, baudrate=19200, parity=serial.PARITY_NONE,
								stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS, timeout=0.5)
	# Flush the inputs and outputs
	serial_port.flushInput()
	serial_port.flushOutput()

	return serial_port



#region CONECTA MQTT					
def conectar(user, password, broker_address, port):
	#Conecta con el servidor MQTT
	global client
	try:
		client = mqttClient.Client(planta+"_Solarmax")               #create new instance
		#client = mqttClient.Client(mqttClient.CallbackAPIVersion.VERSION2, planta+"_LeeSungrowIP")               #create new instance
		client.username_pw_set(user, password=password)    #set username and password
		client.on_connect= on_connect                      #attach function to callback
		client.on_publish = on_publish
		client.connect(broker_address, port=port)          #connect to broker	
		client.loop_start()        #start the loop
	except Exception as e:
		print(e)


#region BD

def guardaBD(datos):
	"""Guarda en la SQLite los datos que se le pasan"""
	with closing(lite.connect('Lecturas.sqlite')) as con:
		with con:
			con.execute(
				"INSERT INTO Lecturas_guardadas_inverters_Solarmax (dato) VALUES (?)",
				(datos,)
			)


def publicar_y_confirmar(dato, timeout=10):
	with _publish_condition:
		resultado = client.publish("General/datos", dato, qos=2)
		if all(hasattr(resultado, attribute) for attribute in
			   ("rc", "wait_for_publish", "is_published")):
			if resultado.rc != mqttClient.MQTT_ERR_SUCCESS:
				return False
			publicacion = resultado
			evento = None
		else:
			rc, mid = resultado
			if rc != mqttClient.MQTT_ERR_SUCCESS:
				return False
			evento = threading.Event()
			_pending_publish_events[mid] = evento

	if evento is not None:
		try:
			return evento.wait(timeout)
		finally:
			with _publish_condition:
				_pending_publish_events.pop(mid, None)

	publicacion.wait_for_publish(timeout=timeout)
	return publicacion.is_published()


def enviadeBD():
	"""Envía los datos que hay en BD"""
	if not Connected:
		return

	try:
		with closing(lite.connect('Lecturas.sqlite')) as con:
			con.row_factory = lite.Row
			rows = con.execute(
				"SELECT id, dato FROM Lecturas_guardadas_inverters_Solarmax"
			).fetchall()
	except lite.Error as e:
		print("Error al leer la cola de SQLite: {}".format(e))
		return

	for row in rows:
		dato_id = row["id"]
		dato = row["dato"]
		print("Envío dato de BD")
		print(dato)
		try:
			# Keep the row until the QoS 2 delivery handshake completes.
			if not publicar_y_confirmar(dato):
				print("Publicación sin confirmar; se conserva el dato en SQLite.")
			else:
				borraDatoBD(dato_id)
		except Exception as e:
			print("Error al enviar el dato {}: {}".format(dato_id, e))
		time.sleep(1)



def borraDatoBD(dato_id):
	"""Borra de la BD el dato con la id pasada."""
	with closing(lite.connect('Lecturas.sqlite')) as con:
		with con:
			con.execute(
				"DELETE FROM Lecturas_guardadas_inverters_Solarmax WHERE id = ?",
				(dato_id,)
			)
	print("Borrada linea de BD con id={}".format(dato_id))

#endregion

#region PRESIONAR TECLA
# Función para verificar si se ha presionado una tecla
def tecla_presionada():
	dr, _, _ = select.select([sys.stdin], [], [], 0)
	return dr

# Configuración para habilitar la detección sin bloquear el input
def config_teclado():
	fd = sys.stdin.fileno()
	old_settings = termios.tcgetattr(fd)
	tty.setcbreak(sys.stdin.fileno())
	return old_settings

# Restaurar configuración original del teclado
def restaurar_teclado(old_settings):
	termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)

# Función para esperar la pulsación de una tecla o continuar después de 10 segundos
def esperar_tecla():
	print("Presione cualquier tecla en los próximos 10 segundos para leer ya...",end='')
	old_settings = config_teclado()
	start_time = time.time()

	while time.time() - start_time < 10:
		print('.',end='')
		sys.stdout.flush()
		if tecla_presionada():
			sys.stdin.read(1)
			restaurar_teclado(old_settings)
			return True
		time.sleep(0.1)  # Pequeña pausa para no consumir demasiado CPU
	
	restaurar_teclado(old_settings)
	print("La primera lectura será al siguiente cuarto.")
	return False
#endregion

def deboLeer():
	"""Devuelve True si es un minuto de los de leer y todavía no ha leído"""
	global leido
	minutos = datetime.now().minute
	#Sólo se tiene que hacer a los cuartos
	print(minutos,end=',')
	sys.stdout.flush()
	if minutos in (0, 15, 30, 45):
		#Sólo lo hago si no lo he leido ya este minuto
		if not leido:
			leido=True #Marco que ha leído este minuto
			return True
		return False
	leido=False
	return False


def esHoraCero():
	"""Devuelve True si la hora actual está entre las 00:00 y las 00:59"""
	return datetime.now().hour == 0

#AQUÍ EMPIEZA EL MAIN
def main():
	global planta, Connected, client, leido
	root = ET.parse('configSerie.xml').getroot()
	planta=root.attrib['Nombre']
	puerto=root.attrib['Puerto']
	datos_inverters = [
		child.find("Numero").text.split(",")
		for child in root
	]


	#Leo la configuración desde el archivo
	cfg = ConfigParser.ConfigParser()
	if not cfg.read("config.cfg"):
	#    logger.warning("No existe el archivo de configuracion")
		sys.exit(0)

	Connected = False   #global variable for the state of the connection
	broker_address= cfg.get("MQTT", "broker_address")
	port = cfg.getint("MQTT", "port")
	user = cfg.get("MQTT", "user")
	password = cfg.get("MQTT", "password")	



	leido=False   

	#Miro si se pulsa una tecla para leer al instante
	if esperar_tecla():
		#Se debe leer ya
		leerYa=True
	else:
		leerYa=False	

	try:	
		while True:
			#Es un minuto de los de leer o le han dado a una tecla al arrancar
			if deboLeer() or leerYa:
				leerYa=False #Ya no debo leer más sin tener en cuenta la hora
				#Reconecto al servidor MQTT
				conectar(user, password, broker_address, port)
				veces=0
				while not Connected and veces < 50:    #Wait for connection
					time.sleep(0.1)
					print("espero reconexión")
					veces+=1
				serial_port = open_serial_port(puerto)
				print("puerto usado: {}".format(serial_port.name))
				try:
					#Recorro los inverters y hago las peticiones de datos
					for inverter in datos_inverters:
						for inverter_numero in inverter:
							try:
								req_data = construct_inverter_query(
									int(inverter_numero),
									[KDY, PAC, KMT, SAL, SYS, IL1, IL2, IL3, IDC,
									 TKK, UL1, UL2, UL3, UDC, UGD]
								)
								if not req_data:
									continue
								print("{}-{}".format(inverter_numero, req_data))
								data = read_inverter(req_data, serial_port, 1)
								print(data)
								if data:
									data = convert_data(data)
									print("Guardo en BD datos del inverter {}".format(inverter_numero))
									print(planta + "/datos")
									print(data)
									print(data['KDY'])
									#Si es la hora cero pongo a cero la energía diaria porque puede salir la del día anterior
									if esHoraCero():
										data['KDY'] = 0
									guardaBD(json.dumps(data))
							except Exception as e:
								print("Error al leer el inverter {}: {}".format(inverter_numero, e))
							time.sleep(2)
				finally:
					serial_port.close()
				#Intento enviar los datos de la BD
				enviadeBD()	
				time.sleep(10)	
				#Desconecto del servidor MQTT
				if Connected:
					print("Desconecto del servidor MQTT")
					client.disconnect()
					client.loop_stop()
					Connected=False
			else:
				time.sleep(10)	
	except KeyboardInterrupt:
		pass
	#	f.close()
		# client.disconnect()
		# client.loop_stop()

if __name__ == "__main__":
	main()
