/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file   app_fatfs.c
  * @brief  Code for fatfs applications
  ******************************************************************************
  * @attention
  *
  * <h2><center>&copy; Copyright (c) 2020 STMicroelectronics.
  * All rights reserved.</center></h2>
  *
  * This software component is licensed by ST under Ultimate Liberty license
  * SLA0044, the "License"; You may not use this file except in compliance with
  * the License. You may obtain a copy of the License at:
  *                             www.st.com/SLA0044
  *
  ******************************************************************************
  */
/* USER CODE END Header */

/* Includes ------------------------------------------------------------------*/
#include "app_fatfs.h"
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include <math.h>
#include "user.h"
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */
typedef enum {
  APPLICATION_IDLE = 0,
  APPLICATION_INIT,
  APPLICATION_RUNNING,
  APPLICATION_SD_UNPLUGGED,
}FS_FileOperationsTypeDef;
/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
FATFS USERFatFs;    /* File system object for USER logical drive */
FIL USERFile;       /* File  object for USER */
char USERPath[4];   /* USER logical drive path */
/* USER CODE BEGIN PV */

FS_FileOperationsTypeDef Appli_state = APPLICATION_IDLE;
fatfs_disk_size dsk_size = {0};

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
/* USER CODE BEGIN PFP */

void test_flash(uint32_t start);

/* USER CODE END PFP */

/**
  * @brief  FatFs initialization
  * @param  None
  * @retval Initialization result
  */
int32_t MX_FATFS_Init(void)
{
  /*## FatFS: Link the disk I/O driver(s)  ###########################*/

if (FATFS_LinkDriver(&USER_Driver, USERPath) != 0)
  /* USER CODE BEGIN FATFS_Init */
	{
		return APP_ERROR;
	}
	else
	{
		uint8_t work[_MAX_SS];
		uint8_t id[3];

		/* Resume from power down mode */
		standardflashResumeFromDPD();

		/* Get manufacturer and part ID */

		standardflashReadMID(id);
		if(id[0] == 0x9f)
		{
			int32_t res = f_mount(&USERFatFs, USERPath, 1);

			if(res == FR_NO_FILESYSTEM)
			  res = f_mkfs(USERPath, FM_FAT, 0, work,_MAX_SS);

			if(res == FR_OK)
				MX_FATFS_ComputeFreeSpaceKB(&dsk_size);

			Appli_state = APPLICATION_INIT;
			return APP_OK;
		}
		else
		{
			return APP_ERROR;
		}
	}

  /* USER CODE END FATFS_Init */
}

/**
  * @brief  FatFs application main process
  * @param  None
  * @retval Process result
  */
int32_t MX_FATFS_Process(void)
{
  /* USER CODE BEGIN FATFS_Process */
  int32_t process_res = APP_OK;

  return process_res;
  /* USER CODE END FATFS_Process */
}

/**
  * @brief  Gets Time from RTC (generated when FS_NORTC==0; see ff.c)
  * @param  None
  * @retval Time in DWORD
  */
DWORD get_fattime(void)
{
  /* USER CODE BEGIN get_fattime */
	uint32_t ul_time;
	
	/* Retrieve date and time */
	struct tm *p_datetime = GetDateTime_st();

	ul_time = ((p_datetime->tm_year - 80) << 25)
			| ((p_datetime->tm_mon + 1) << 21)
			| (p_datetime->tm_mday << 16)
			| (p_datetime->tm_hour << 11)
			| (p_datetime->tm_min << 5)
			| ((p_datetime->tm_sec >> 1) << 0);

	return ul_time;
  /* USER CODE END get_fattime */
}

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN Application */

void test_flash(uint32_t start)
{
	uint32_t len = 256;
	volatile uint32_t i, st_addr = start;
	uint8_t err = 0, r_buf[256], w_buf[256];

	for (uint32_t i = 1; i < 256; i++)
	{
		w_buf[i] = (uint8_t)i;
		r_buf[i] = 0x55;
	}

	standardflashWriteEnable();
	standardflashBlockErase4K(st_addr);

	standardflashWaitOnReady();
	standardflashReadArrayLowFreq(st_addr, r_buf, len);

	standardflashWriteEnable();
	standardflashBytePageProgram(st_addr, w_buf, len);

	/* Verify write page */
	standardflashWaitOnReady();
	standardflashReadArrayLowFreq(st_addr, r_buf, len);

	for(i = 0; i < len; i++)
	{
		if(r_buf[i] !=  w_buf[i])
			err++;
	}

	standardflashWriteDisable();

}

FRESULT MX_FATFS_ComputeFreeSpaceKB(fatfs_disk_size *disk_size)
{
	uint32_t fre_clust, c_size;;
	FATFS *fsAux;
	FRESULT res= f_getfree("0:", &fre_clust, &fsAux);

    if (res == FR_OK)
	{
    	float f_v;
		c_size = fsAux->csize * _MAX_SS;
		disk_size->freeKB = fre_clust * c_size;
		disk_size->totalKB = (fsAux->n_fatent - 2) * c_size;
		f_v = 100.0 * ((float)disk_size->freeKB / (float)disk_size->totalKB);
		disk_size->free_percent =  (uint16_t)lrintf(f_v);
	}
	return res;
}

fatfs_disk_size *MX_FATFS_GetFreeSpaceKB(void)
{
	return &dsk_size;
}


/* USER CODE END Application */
