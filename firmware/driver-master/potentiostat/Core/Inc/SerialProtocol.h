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

/* A received frame is complete once the line has been silent for longer than
 * this many milliseconds. */
#define FRAME_SILENCE_MS 2u

// Function prototypes
uint32_t Compute_Serial_Cmd(uint32_t total);          // Parse and execute one received frame
void Serial_Poll(void);                               // Main-loop entry: run a frame once it is complete

#else
	#warning "SErialProtocol.h have been defined!"
#endif /* MODBUSRTU_H_ */

