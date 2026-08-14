/*
 * QueryList.h
 *
 *  Created on: 28 sept. 2018
 *      Author: agarcia
 */

#ifndef QUERYLIST_H_
#define QUERYLIST_H_

#define CMD_RX_BUFFER_SIZE 24000
#define CMD_TX_BUFFER_SIZE 64100

enum
{
	CMD_ANALOG_READ = 1,
	CMD_DAC_WRITE = 2,
	CMD_DAC_READ = 3,
	CMD_SWITCH_WRITE = 4,
	CMD_TIA_GAIN_WRITE = 5,
	CMD_CFG_DATETIME = 6,
	CMD_DAC_EXECUTE_BATCH = 7,
	CMD_BUFFER_WRITE = 8,
	CMD_BUFFER_READ = 9,
	CMD_CURRENT_HOLD = 10,
	CMD_BUFFER_RESET = 11,
	CMD_SAMPLE_COUNT = 12,
	CMD_AUTO_GAIN_WRITE = 13,
	CMD_ANALOG_GAIN_READ = 14,
	CMD_AUTO_GAIN_READ = 15,
	CMD_TIA_GAIN_READ = 16,
	CMD_SWITCH_READ = 17,
	CMD_CURRENT_HOLD_STOP = 18,
	CMD_DIAGNOSTICS_READ = 19,
	/* Self-framing, position-explicit waveform buffer write:
	 * [ID][20][u16 dlen][u16 pos][dlen bytes]. Unlike CMD_BUFFER_WRITE it
	 * survives being merged with a following message in one RX frame (the
	 * parser walks it by its own length) and a lost message cannot shift
	 * the ring alignment (the host names the write position). */
	CMD_BUFFER_WRITE_AT = 20,
};

typedef struct _serialQuery
{
	uint16_t cmd_type;
	uint16_t length;
	void *pdata;
	int32_t (*pfunc)(void *,uint32_t);
}serialQuery;

serialQuery *Get_CfgList(void);
uint32_t GetListSize(void);
int32_t ProcessDacWriteBatchStep();
int32_t ProcessBufferWriteAt(void *pData, uint32_t data_len);

extern uint8_t gain_val;
//Batch related variables
extern uint32_t buffer_ind;
extern uint32_t cmd_ind;
extern uint32_t cmd_count;
extern uint32_t cmd_step_rx_size;
extern uint32_t cmd_step_tx_size;
extern uint32_t cmd_step_delay;
extern uint32_t cmd_last_tick;
extern uint8_t cmd_rx_buffer[CMD_RX_BUFFER_SIZE];
extern uint8_t cmd_tx_buffer[CMD_TX_BUFFER_SIZE];
extern uint32_t batch_bytes_written;
extern uint32_t batch_starved_steps;
extern uint32_t analog_sample_count;

#endif /* QUERYLIST_H_ */
