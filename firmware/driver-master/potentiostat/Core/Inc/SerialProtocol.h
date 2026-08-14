/*
 * ModbusRTU.c
 *
 * Created: 15/08/2023
 *  Author: deniz
 */ 

#ifndef SERIALPROTOCOL_H_
#define SERIALPROTOCOL_H_

#define SERIAL_RX_BUFFER_SIZE 4096
extern uint8_t serial_rx_buffer[SERIAL_RX_BUFFER_SIZE];

typedef enum _exception{NO_ERROR=0,ILLEGAL_FUNCTION=1,ILLEGAL_DATA_SIZE=2,
	ILLEGAL_DATA_VALUE=3,} exception;

//typedef int32_t (*serial_response_fn)(void *data, uint16_t length, uint8_t port);

// Function prototypes
uint32_t Compute_Serial_Cmd();                        // Mod bus master request computing

#else
	#warning "SErialProtocol.h have been defined!"
#endif /* MODBUSRTU_H_ */

