/*
 * user.c
 *
 *  Created on: 7 nov. 2017
 *      Author: agarcia
 */

#include "user.h"
#include "usbd_cdc_if.h"

__attribute__ ((section (".bootram_data")))stBoot RstFlag;

__attribute__ ((section(".firm_version")))stVersion FirmVersion =
{
		.major = 1,
		.minor = 1,		// Added diagnostics-read command (backward-compatible)
		.patch = 0,		// MOLES-modified build (USB stall/CV write-timeout fixes)
		.pcb_rev = 'A',
		.pcb_var ='1',
		.TimeStamp = __TIMESTAMP__,
};

const uint8_t dflt_passwort[] = "Poten1!";

stDeviceConfig dev_cfg = {.Password.Accesslevel.Reg = 0x07ff};

stHoldindReg HldRegs;

u_chipserial chip_serial;
u_chip_ID64 chip_ID64;
RTC_HandleTypeDef *p_hrtc;
struct tm sys_dtime;

uint32_t bSaveNVM = 0;

/* USB cdc to serial variable definition */

uint32_t usb_rx_tmr = 0;	// Last communication time in ms
uint32_t rcv_ix = 0;		// Received bytes counter
uint8_t serial_rx_buffer[SERIAL_RX_BUFFER_SIZE]={0};	// Received bytes buffer
osThreadId_t modbus_task_hldr;	// USB to modbus thread id


stBoot *GetRstFlag(void)
{
	return (stBoot *)&RstFlag;
}

uint8_t *GetChipSerial(void)
{
	chip_serial.Serial32[0] = *((uint32_t *)UID_BASE);
	chip_serial.Serial32[1] = *((uint32_t *)(UID_BASE + 4));
	chip_serial.Serial32[2] = *((uint32_t *)(UID_BASE + 8));
	chip_serial.Serial32[3] = 0;
	return chip_serial.Serial8;
}

uint8_t *GetChipID64(void)
{
	chip_ID64.dword[0] = *((uint32_t *)UID_BASE);
	chip_ID64.byte[4] = *((uint32_t *)(UID_BASE + 4));
	chip_ID64.byte[5] = *((uint32_t *)(UID_BASE + 8));
	chip_ID64.byte[6] = *((uint32_t *)(UID_BASE + 9));
	chip_ID64.byte[7] = *((uint32_t *)(UID_BASE + 10));
	return chip_ID64.byte;
}

struct tm *GetDateTime_st(void)
{
	return &sys_dtime;
}

stDeviceConfig *GetDeviceConfig(void)
{
	return &dev_cfg;
}
uint8_t *GetDfltPass(void)
{
	return (uint8_t *)dflt_passwort;
}

stHoldindReg *GetHoldingReg(void)
{
	return &HldRegs;
}

struct tm *GetSysDateTime(void)
{
	return &sys_dtime;
}

void delay_us(uint32_t delay)
{
	volatile uint32_t t_start = TIM5->CNT;

	if(TIM5->CR1 & TIM_CR1_CEN)
		while(TimeDiff(t_start, TIM5->CNT) < delay);
}

uint32_t GetTick_us(void)
{
	return TIM5->CNT;
}

uint32_t GetUint32FromBuffer(uint8_t *p_d)
{
	return (uint32_t)((*p_d <<24) | (*(p_d + 1) <<16) | (*(p_d + 2) <<8) | *(p_d + 3));
}

uint64_t GetUint64FromBuffer(uint8_t *p_d)
{
	uint32_t l_w, h_w;

	h_w = (*p_d <<24) | (*(p_d + 1) <<16) | (*(p_d + 2) <<8) | *(p_d + 3);
	l_w = (*(p_d + 4) <<24) | (*(p_d + 5) <<16) | (*(p_d + 6) <<8) | *(p_d + 7);

	return (uint64_t)((((uint64_t)h_w) <<32) | l_w);
}

/* Compute the CRC of data buffer pointed by *data with length in bytes */
uint16_t CRC16_compute(uint8_t *data, uint16_t length)
{
	CRC_HandleTypeDef h_crc;
	uint16_t crc16 = 0;

	h_crc.Instance = CRC;
	h_crc.Init.DefaultPolynomialUse = DEFAULT_POLYNOMIAL_DISABLE;
	h_crc.Init.DefaultInitValueUse = DEFAULT_INIT_VALUE_DISABLE;
	h_crc.Init.GeneratingPolynomial = 0x8005;
	h_crc.Init.CRCLength = CRC_POLYLENGTH_16B;
	h_crc.Init.InitValue = 0xffff;
	h_crc.Init.InputDataInversionMode = CRC_INPUTDATA_INVERSION_BYTE;
	h_crc.Init.OutputDataInversionMode = CRC_OUTPUTDATA_INVERSION_ENABLE;
	h_crc.InputDataFormat = CRC_INPUTDATA_FORMAT_BYTES;

	if(HAL_CRC_Init(&h_crc) == HAL_OK)
		crc16 = HAL_CRC_Calculate(&h_crc, (uint32_t *)data, length);

	return crc16;
}

uint32_t CRC32_compute(uint8_t *buffer, uint32_t size)
{
	CRC_HandleTypeDef h_crc;
	uint32_t crc32 = 0;

	h_crc.Instance = CRC;
	h_crc.Init.DefaultPolynomialUse = DEFAULT_POLYNOMIAL_DISABLE;
	h_crc.Init.DefaultInitValueUse = DEFAULT_POLYNOMIAL_DISABLE;
	h_crc.Init.GeneratingPolynomial = 0x04C11DB7;
	h_crc.Init.CRCLength = CRC_POLYLENGTH_32B;
	h_crc.Init.InitValue = 0xFFFFFFFF;
	h_crc.Init.InputDataInversionMode = CRC_INPUTDATA_INVERSION_BYTE;
	h_crc.Init.OutputDataInversionMode = CRC_OUTPUTDATA_INVERSION_ENABLE;
	h_crc.InputDataFormat = CRC_INPUTDATA_FORMAT_BYTES;

	/* Compute CRC32 and make One Complement crc32 */
	if (HAL_CRC_Init(&h_crc) == HAL_OK)
		crc32 = ~HAL_CRC_Calculate(&h_crc, (uint32_t *)buffer, size);

	return crc32;
}

/*******************************************************************
	Method      :  TimeDiff

	Description :
         This function gets the difference between two time stamp, time
         overflow is considered.
**********************************************************************/

uint32_t TimeDiff(uint32_t time_start, uint32_t time_end)
{
	return TIMEDIFF(time_start, time_end);
}

/*******************************************************************
	Method      :  TimeExpired

	Description :
         This function get ellapsed time from time_start
**********************************************************************/

uint32_t EllapsedTime(uint32_t time_start)
{
	return TIMEDIFF(time_start, HAL_GetTick());
}

/*******************************************************************
	Method      :  TimeExpired

	Description :
         This function check if an interval time was expired
**********************************************************************/

uint32_t TimeExpired(uint32_t start, uint32_t interval)
{
	return (TIMEDIFF(start, HAL_GetTick()) > interval);
}


/* 3 digits BIN to BCD converter */

uint32_t fBin2BCD(uint8_t u)
{
	uint8_t c,d;

    for(c = 0; u >= 100; u -= 100, c++);
    for(d = 0; u >= 10; u -= 10, d++);
    return (uint32_t)((c << 8) | (d << 4) | u);
}

/* 3 digits BCD to BIN converter */

uint32_t fBCD2Bin(uint8_t n)
{
    return (uint32_t)((100 * (n & 0xf00) >>8) + (10 * (n & 0xf0) >>4) + (n & 0xf));
}

void StructTm_To_PackDt(PacketTime *pPacktDT, struct tm *pDT)
{
	pPacktDT->sec = (uint8_t)pDT->tm_sec;
	pPacktDT->min = (uint8_t)pDT->tm_min;
	pPacktDT->hour = (uint8_t)pDT->tm_hour;
	pPacktDT->day = (uint8_t)pDT->tm_mday;
	pPacktDT->mon = (uint8_t)(pDT->tm_mon + 1);
	pPacktDT->year = (uint8_t)(pDT->tm_year + (CUT_YEAR - RTC_CENTURY));
}

void PackDT_To_StructTm(PacketTime *pPacktDT, struct tm *pDT)
{
	pDT->tm_sec = (int32_t)pPacktDT->sec;
	pDT->tm_min = (int32_t)pPacktDT->min;
	pDT->tm_hour = (int32_t)pPacktDT->hour;
	pDT->tm_mday = (int32_t)pPacktDT->day;
	pDT->tm_mon  = (int32_t)(pPacktDT->mon - 1);
	pDT->tm_year = (int32_t)(pPacktDT->year + (RTC_CENTURY - CUT_YEAR));
}

void GetDatetimeFromIRTC(struct tm *p_dt)
{
	RTC_DateTypeDef date_def;
	RTC_TimeTypeDef time_def;

	if(p_hrtc->Instance->ICSR & RTC_ICSR_RSF)
	{
		/* Call first get time to avoid long update time lapsus */
		HAL_RTC_GetTime(p_hrtc, &time_def, RTC_FORMAT_BIN);
		HAL_RTC_GetDate(p_hrtc, &date_def, RTC_FORMAT_BIN);

		p_dt->tm_hour = time_def.Hours + p_dt->tm_isdst;
		p_dt->tm_min = time_def.Minutes;
		p_dt->tm_sec = time_def.Seconds;

		p_dt->tm_mday = date_def.Date;
		p_dt->tm_mon = date_def.Month - RTC_MONTH_JANUARY;
		p_dt->tm_year = (uint8_t)(date_def.Year + (RTC_CENTURY - CUT_YEAR));
		p_dt->tm_wday = date_def.WeekDay - RTC_WEEKDAY_MONDAY;
	}
}

void DateTime_Init(void *rtc)
{
	p_hrtc = rtc;
}

void SetDatetimeToInternalRTC(struct tm *p_dt)
{
	if(p_hrtc != NULL)
	{
		int32_t result = HAL_OK;
		int32_t hour = p_dt->tm_hour - p_dt->tm_isdst;
		RTC_DateTypeDef date_def;
		RTC_TimeTypeDef time_def;

		if(hour < 0)
			hour = 23;

		time_def.Hours = hour;
		time_def.Minutes= p_dt->tm_min ;
		time_def.Seconds= p_dt->tm_sec ;
		time_def.SubSeconds = 0;
		time_def.DayLightSaving = RTC_DAYLIGHTSAVING_NONE;
		time_def.StoreOperation = RTC_STOREOPERATION_RESET;
		result = HAL_RTC_SetTime(p_hrtc, &time_def, RTC_FORMAT_BIN);

		date_def.Date = p_dt->tm_mday;
		date_def.Month = p_dt->tm_mon + RTC_MONTH_JANUARY;
		date_def.Year = (uint8_t)(p_dt->tm_year + (CUT_YEAR - RTC_CENTURY));
		date_def.WeekDay = p_dt->tm_wday + RTC_WEEKDAY_MONDAY;
		result |= HAL_RTC_SetDate(p_hrtc, &date_def, RTC_FORMAT_BIN);
	}
}

int32_t Update_DateTime(void *p_var)
{
	struct tm *p_dt = p_var;
    GetDatetimeFromIRTC(p_dt);
    return SUCCESS;
}

int32_t Read_I2C_Buffer(void *handler, uint8_t addr, void *buffer, uint16_t length)
{
	return HAL_I2C_Master_Receive((I2C_HandleTypeDef *)handler, addr,(uint8_t *)buffer, length, 1500);
}

int32_t Write_I2C_Buffer(void *handler, uint8_t addr, void *buffer, uint16_t length)
{
	return HAL_I2C_Master_Transmit((I2C_HandleTypeDef *)handler, addr, (uint8_t *)buffer, length, 1500);
}

void Humidity_Sensor_Task(void *i2c_hdlr)
{

	uint16_t id;
	shrc3_ctx ht_ctx = {
			(SHRC3_ADDR <<1),
			i2c_hdlr,
			Write_I2C_Buffer,
			Read_I2C_Buffer};

	int32_t res = HAL_ERROR;

	if(i2c_hdlr == NULL)
		return;

	SHTC3_Init(&ht_ctx);

	for(int x = 0; (x < 20) && (res != HAL_OK); ++x)
	{
		res = HAL_I2C_Init((I2C_HandleTypeDef *)i2c_hdlr);
		if(res == HAL_OK)
		{
			res = SHTC3_Wakeup();
			res |= SHTC3_GetId(&id);
			res = ((res == HAL_OK) && ((id & ID_MASK) == ID_VALUE)) ? HAL_OK : HAL_ERROR;

			if(res == HAL_OK)
			{
				float temperature, humidity;
				SHTC3_GetTempAndHumiPolling(&temperature, &humidity);
			}
		}

	}
}

/* Send modbus response */

int32_t CDC_Response_Frame(void *data, uint16_t length, uint8_t port)
{
	return (int32_t)CDC_Transmit_FS(data, length);
}

/* Process received usb modbus data frame */


