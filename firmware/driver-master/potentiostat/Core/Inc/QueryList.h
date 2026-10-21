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
	/* [u32 step delay ms][u32 step count][optional u32 option flags]:
	 * run the uploaded waveform, one setpoint per step on an absolute
	 * millisecond grid. Flag bit 0 samples each step at the end of its dwell
	 * (otherwise right after the setpoint is written); bit 1 ends the batch
	 * with the cell disconnected if waveform data stops arriving for 2 s;
	 * bit 2 (with bit 0) changes current range as the current changes, only
	 * at points that are a multiple of the period in bits 16-31, and tags
	 * every sample with its range. Two-word hosts get none of these. A
	 * count of 0 halts the running batch. */
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
	/* Step-response capture for the R_u measurement:
	 * [u32 n_samples][u32 period_us] -> close the CE switch and burst-sample
	 * WE_OUT raw counts into cmd_tx_buffer at a cycle-counter-timed pace.
	 * Responds [u32 captured][u32 actual_duration_us][u8 gain] after the
	 * capture; the host then collects the trace with CMD_BUFFER_READ. */
	CMD_STEP_CAPTURE = 21,
	/* The same capture with the step made by the DAC instead of the switch:
	 * [u32 n_samples][u32 period_us][u32 n_pre][f32 dac_V] -> with the cell
	 * already connected, sample n_pre readings, move the CE DAC to dac_V,
	 * keep sampling. Responds [u32 captured][u32 actual_duration_us]
	 * [u8 gain][u32 centre_ns]; captured is 0 when the cell is not
	 * connected. */
	CMD_DAC_STEP_CAPTURE = 22,
	/* On-board iR compensation for batches run with BATCH_FLAG_IR_COMP:
	 * [f32 k[5]][f32 max_dac][f32 max_step_dac][f32 alpha]
	 * [u32 runaway_steps][u32 limit_steps], acknowledged "OK" (nothing, if
	 * a value is out of range). */
	CMD_IR_CONFIG = 23,
	/* Its run report since the last buffer reset: [u32 batches compensated]
	 * [u32 steps at the cap or DAC end][u32 steps trailing at the walking
	 * limit][u32 runaway aborts][u32 limit aborts][f32 largest correction]
	 * [f32 correction now], DAC volts. */
	CMD_IR_REPORT = 24,
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
int32_t ProcessStepCapture(void *pData, uint32_t data_len);
int32_t ProcessDacStepCapture(void *pData, uint32_t data_len);
int32_t ProcessIrConfig(void *pData, uint32_t data_len);
int32_t ProcessIrReport(void *pData, uint32_t data_len);

extern uint8_t gain_val;
//Batch related variables
extern uint32_t buffer_ind;
extern uint32_t cmd_ind;
extern uint32_t cmd_count;
extern uint32_t cmd_step_rx_size;
extern uint32_t cmd_step_tx_size;
extern uint32_t cmd_step_delay;
extern uint32_t cmd_due_tick;
extern uint8_t cmd_rx_buffer[CMD_RX_BUFFER_SIZE];
extern uint8_t cmd_tx_buffer[CMD_TX_BUFFER_SIZE];
extern uint32_t batch_bytes_written;
extern uint32_t batch_starved_steps;
extern uint32_t batch_rejected_setpoints;
extern uint32_t analog_sample_count;

#endif /* QUERYLIST_H_ */
