package com.ruoyi.alarm.service;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmApp;

/**
 * 预警应用 Service接口
 *
 * @author ruoyi
 */
public interface IAlarmAppService
{
    /**
     * 查询预警应用列表
     *
     * @param alarmApp 预警应用
     * @return 预警应用集合
     */
    public List<AlarmApp> selectAlarmAppList(AlarmApp alarmApp);

    /**
     * 查询预警应用
     *
     * @param appId 应用ID
     * @return 预警应用
     */
    public AlarmApp selectAlarmAppById(Long appId);

    /**
     * 根据应用ID查询预警应用
     *
     * @param appId 应用ID
     * @return 预警应用
     */
    public AlarmApp selectAlarmAppByAppId(Long appId);

    /**
     * 校验应用身份
     *
     * @param appId 应用ID
     * @param appSecret 应用秘钥
     * @return 是否合法
     */
    public boolean validateApp(Long appId, String appSecret);

    /**
     * 新增预警应用
     *
     * @param alarmApp 预警应用
     * @return 结果
     */
    public int insertAlarmApp(AlarmApp alarmApp);

    /**
     * 修改预警应用
     *
     * @param alarmApp 预警应用
     * @return 结果
     */
    public int updateAlarmApp(AlarmApp alarmApp);

    /**
     * 删除预警应用
     *
     * @param appId 应用ID
     * @return 结果
     */
    public int deleteAlarmAppById(Long appId);

    /**
     * 批量删除预警应用
     *
     * @param appIds 应用ID数组
     * @return 结果
     */
    public int deleteAlarmAppByIds(Long[] appIds);
}
