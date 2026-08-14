//==============================================================================
//    S E N S I R I O N   AG,  Laubisruetistr. 50, CH-8712 Staefa, Switzerland
//==============================================================================
// Project   :  SHTC3 Sample Code (V1.0)
// File      :  shtc3.c (V1.0)
// Author    :  RFU
// Date      :  24-Nov-2017
// Controller:  STM32F100RB
// IDE       :  �Vision V5.17.0.0
// Compiler  :  Armcc
// Brief     :  Sensor Layer: Implementation of functions for sensor access.
//==============================================================================

#include "user.h"
#include <cmsis_os2.h>

static etError SHTC3_Read2BytesAndCrc(uint16_t *data);
static etError SHTC3_WriteCommand(etCommands cmd);
static etError SHTC3_CheckCrc(uint8_t data[], uint8_t nbrOfBytes, uint8_t checksum);
static float SHTC3_CalcTemperature(uint16_t rawValue);
static float SHTC3_CalcHumidity(uint16_t rawValue);

static shrc3_ctx *p_ctx;

void SHTC3_Init(shrc3_ctx *dev_ctx)
{
	p_ctx = dev_ctx;
}

etError SHTC3_GetTempAndHumiPolling(float *temp, float *humi)
{
	etError  res;           // error code

	shtc3_raw_ht ht_data;

	// measure, read temperature first, clock streching disabled (polling)
	res = SHTC3_WriteCommand(MEAS_T_RH_POLLING);

	if(res != SHTC3_NO_ERROR)
		return res;

	res = SHTC3_ACK_ERROR;

	// poll every 1ms for measurement ready
	for(int x =  0; (x < MAX_POLLING_RETRIES) && (res != SHTC3_NO_ERROR); --x, osDelay(1))
		res = p_ctx->read_reg(p_ctx->handler, p_ctx->addr, &ht_data, sizeof(ht_data));

	// if no error, calculate temperature in ºC and humidity in %RH
	if(res == SHTC3_NO_ERROR)
	{
		*temp = SHTC3_CalcTemperature(swap16(ht_data.temperature));
		*humi = SHTC3_CalcHumidity(swap16(ht_data.humidity));
	}
	return res;
}

etError SHTC3_GetId(uint16_t *id){
  etError res = SHTC3_WriteCommand(READ_ID);

  if(res == SHTC3_NO_ERROR)
	  SHTC3_Read2BytesAndCrc(id);

  return res;
}

etError SHTC3_Sleep(void) {
  return SHTC3_WriteCommand(SLEEP);
}

etError SHTC3_Wakeup(void) {
  etError error = SHTC3_WriteCommand(WAKEUP);

  osDelay(1); // wait 1ms

  return error;
}

etError SHTC3_SoftReset(void){

  // write reset command

	return SHTC3_WriteCommand(SOFT_RESET);;
}

static etError SHTC3_WriteCommand(etCommands cmd)
{
  uint16_t cmd_buf = swap16(cmd);

  // Timeout = Ti2cclk * 2048 * Timeout (12bit) = Timeout * 12us for 170MHz
  return p_ctx->write_reg(p_ctx->handler, p_ctx->addr, &cmd_buf, sizeof(cmd_buf));

}

static etError SHTC3_Read2BytesAndCrc(uint16_t *data)
{
  uint8_t rd_buf[3]; // read data array

  int32_t res = p_ctx->read_reg(p_ctx->handler, p_ctx->addr, rd_buf, sizeof(rd_buf));

  if(res == SHTC3_NO_ERROR)
  {
	  // verify checksum that's 3 byte
	  res = SHTC3_CheckCrc(rd_buf, 2,  rd_buf[2]);

	  // combine the two data bytes to a 16-bit value
	  if(res == SHTC3_NO_ERROR)
		  *data = (rd_buf[0] << 8) | rd_buf[1];
  }

  return res;
}

static etError SHTC3_CheckCrc(uint8_t data[], uint8_t nbrOfBytes, uint8_t checksum)
{
  uint8_t bit;        // bit mask
  uint8_t crc = 0xFF; // calculated checksum
  uint8_t byteCtr;    // byte counter

  // calculates 8-Bit checksum with given polynomial
  for(byteCtr = 0; byteCtr < nbrOfBytes; byteCtr++) {
    crc ^= (data[byteCtr]);
    for(bit = 8; bit > 0; --bit) {
      if(crc & 0x80) {
        crc = (crc << 1) ^ CRC_POLYNOMIAL;
      } else {
        crc = (crc << 1);
      }
    }
  }

  // verify checksum
  if(crc != checksum) {
    return SHTC3_CHKSUM_ERROR;
  } else {
    return SHTC3_NO_ERROR;
  }
}

//------------------------------------------------------------------------------
static float SHTC3_CalcTemperature(uint16_t rawValue){
  // calculate temperature [�C]
  // T = -45 + 175 * rawValue / 2^16
  return 175 * (float)rawValue / 65536.0f - 45.0f;
}

//------------------------------------------------------------------------------
static float SHTC3_CalcHumidity(uint16_t rawValue){
  // calculate relative humidity [%RH]
  // RH = rawValue / 2^16 * 100
  return 100 * (float)rawValue / 65536.0f;
}
