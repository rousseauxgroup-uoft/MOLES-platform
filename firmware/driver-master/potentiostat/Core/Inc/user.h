/*
 * user.h
 *
 *  Created on: 7 nov. 2017
 *      Author: agarcia
 */

#ifndef USER_H_
#define USER_H_

#include "main.h"
#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>
#include <string.h>
#include <math.h>
#include <stm32g4xx_ll_gpio.h>
#include "app_fatfs.h"
#include "analog.h"
#include "LedFlash.h"
#include "shtc3.h"
#include "usbd_cdc_if.h"
#include "standardflash.h"
#include <SerialProtocol.h>
#include "QueryList.h"


#define LED_RED		0
#define LED_GREEN	1
#define LED_BLUE	2

#define USB_PORT 0

#ifndef CUT_YEAR
#define CUT_YEAR		   1900			//  00:00:00 on January 1, 1900, Coordinated Universal Time  ISO/IEC 9899:1999
#endif

#ifndef RTC_CENTURY
#define RTC_CENTURY	       2000			// Base RTC century
#endif

#define MODE_ENCRYPT	ESP_AES_ENCRYPT
#define MODE_DECRYPT	ESP_AES_DECRYPT

#define TIMEDIFF(st, ed)	((ed >= st) ? (ed - st) :  ((ed - st) + (UINT32_MAX  + 1)))

#ifndef	MAX
#define MAX(a, b)  (((a) > (b)) ? (a) : (b))
#endif

#ifndef	MIN
#define MIN(a, b)  (((a) < (b)) ? (a) : (b))
#endif

#ifndef	CLAMP
#define CLAMP(x, low, high)  (((x) > (high)) ? (high) : (((x) < (low)) ? (low) : (x)))
#endif

#ifndef	OUTWARD
#define OUTWARD(v, min, max) ((v < min) || (v > max))
#endif

#ifndef	INWARD
#define INWARD(v, min, max)	!((v > min) || (v > max))
#endif

#ifndef ELEMEN_CNT
#define ELEMEN_CNT(arr)		(sizeof (arr) / sizeof ((arr)[0]))
#endif

#define TXT_BLACK 	"\033[0;30m"
#define TXT_RED 	"\033[0;31m"
#define TXT_GREEN 	"\033[0;32m"
#define TXT_YELLOW 	"\033[0;33m"
#define TXT_BLUE 	"\033[0;34m"
#define TXT_PURPLE 	"\033[0;35m"
#define TXT_CYAN 	"\033[0;36m"
#define TXT_WHITE 	"\033[0;37m"
#define TXT_DEFLT	"\033[0m"

#define swap16(x)	((uint16_t) ((((x) >> 8) & 0xff) | (((x) & 0xff) << 8)))

#define swap32(x)	((uint32_t)((((x) & 0xff000000u) >> 24)| \
				   	  (((x) & 0x00ff0000u) >> 8)|	\
					  (((x) & 0x0000ff00u) << 8)| 	\
					  (((x) & 0x000000ffu) << 24)))

/* Swap bytes in 64-bit value.  */
#define swap64(x)	((uint64_t)((((x) & 0xff00000000000000ull) >> 56)\
		 	 	 	 (((x) & 0x00ff000000000000ull) >> 40) |    \
   					 (((x) & 0x0000ff0000000000ull) >> 24) |	\
   					 (((x) & 0x000000ff00000000ull) >> 8)  |    \
   					 (((x) & 0x00000000ff000000ull) << 8)  |    \
   					 (((x) & 0x0000000000ff0000ull) << 24) |    \
   					 (((x) & 0x000000000000ff00ull) << 40) |    \
					 (((x) & 0x00000000000000ffull) << 56)))

#define swap_u16 swap16
#define swap_u32 swap32
#define swap_u64 swap32

#define  GetTickCount() HAL_GetTick()

/* CRC16  polynomial definition  */

#define	P_CRC16			0xA001
#define INIT_CRC16		0xFFFF


typedef struct
{
	union
	{
		struct
		{
			uint8_t BaudRate;
			uint8_t WaitSec;
			uint8_t BootCmd;
			uint8_t SlaveID;
		}Bootreg;
		uint32_t Reg32;
	};
	uint32_t deadbeef;
}stBoot;

/* Structure for firmware version data*/

typedef struct
{
	uint8_t patch;			// Bug fixes which are also backward compatible
	uint8_t minor;			// Adding new functionality in a backward-compatible manner.
	uint8_t major;			// Incompatible API changes
	uint8_t dummy_byte;		// For packing consistency

	uint8_t pcb_rev;		// PCB revision A-Z character
	uint8_t pcb_var;		// PCB variant 1-9 character

	uint8_t TimeStamp[24];
}stVersion;

typedef struct
{
	union
	{
		struct
		{
			uint32_t rd_hld:1;
			uint32_t wr_hld :1;
			uint32_t rd_cfg :1;
			uint32_t wr_cfg :1;
			uint32_t rd_firm :1;
			uint32_t wr_firm :1;
			uint32_t :1;
			uint32_t :1;
			uint32_t rd_password :1;
			uint32_t wr_password:1;
			uint32_t Master_EN:1;
			uint32_t :22;
		}bits;
		uint32_t Reg;
	}Accesslevel;
	uint32_t MasterPass_tm;			// Master password expiration time
	uint8_t cfg_password[8];		// Password to access and modify configuration parameters
	uint32_t l_access_reg;
	uint32_t master_st_tm;
}stPassword;

typedef struct tag_PacketTime
{
	uint8_t sec;
	uint8_t min;
	uint8_t hour;	//  0-23
	uint8_t day;	// 1 to 31
	uint8_t mon;	// 1 January - 12 December
	uint8_t year;	// 0-99
}PacketTime;


typedef struct tag_stSystemConfig
{
	stPassword Password;
	uint16_t Checksum;
}stDeviceConfig;


typedef struct  tag_stHoldindReg
{
	PacketTime packet_dt;
	struct
	{
		uint16_t IN;
		uint16_t OUT;
	}DIO;

	uint16_t status;
	uint16_t error;

	uint32_t checksum;
}stHoldindReg;


typedef union
{
	uint8_t Serial8[16];
	uint32_t Serial32[4];
}u_chipserial;

typedef union
{
	uint8_t byte[8];
	uint32_t dword[2];
}u_chip_ID64;

stBoot *GetRstFlag(void);
uint8_t *GetChipSerial(void);
uint8_t *GetChipID64(void);

struct tm *GetDateTime_st(void);
stDeviceConfig *GetDeviceConfig(void);
uint16_t CRC16_compute(uint8_t *, uint16_t);
uint32_t CRC32_compute(uint8_t *, uint32_t);
uint8_t *GetDfltPass(void);
stHoldindReg *GetHoldingReg(void);
struct tm *GetSysDateTime(void);
void delay_us(uint32_t);
uint32_t GetTick_us(void);
uint32_t GetUint32FromBuffer(uint8_t *);
uint64_t GetUint64FromBuffer(uint8_t *);
void trigSaveNWM(void);
uint32_t TimeDiff(uint32_t , uint32_t);
uint32_t EllapsedTime(uint32_t);
uint32_t TimeExpired(uint32_t start, uint32_t interval);
uint32_t Dig8_Bin2BCD(uint32_t);
uint32_t fBin2BCD(uint8_t);
uint32_t fBCD2Bin(uint8_t);
void StructTm_To_PackDt(PacketTime *, struct tm *);
void PackDT_To_StructTm(PacketTime *, struct tm *);
void switch_off(void);
void GetDatetimeFromIRTC(struct tm *);
void DateTime_Init(void *);
void SetDatetimeToInternalRTC(struct tm *);
int32_t Update_DateTime(void *p_var);
void Humidity_Sensor_Task(void *i2c_hdlr);
int32_t CDC_Response_Frame(void *, uint16_t, uint8_t);
void BlinkLed_Compute(stLedConfig *pLed);
#endif /* USER_H_ */
